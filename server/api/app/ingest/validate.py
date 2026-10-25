"""The ingest payload (docs/live-upload.md 5): decode, validate, convert.

A batch is gzip-JSON: an envelope (v, batch_seq, buffer_id, buffer_version,
sent_at, attempt, heartbeat, boots, gaps) and `tables`, each {cols, rows} with
the rows as arrays in the order of cols. cols must equal the staging table's
columns (ingest_stage_create() in schema.sql - a test keeps TABLES equal to
it); the rows go into it by COPY unchanged, except load sample times
(timestamptz in the stage). Only the tables the live ingest may write are
known here: never associations, client_bssids or probes (D3), and a device
row must be an AP by the ap_evidence() rule (privacy: a sensor never uploads
a client device). Times are unix seconds on the corrected time line
(8.3): not before MIN_TS, not more than FUTURE_S ahead of the server (a wrong
clock is refused, not stored). Keys are unique per table (the merge's DO
UPDATE would refuse a key twice).

Errors: BadRequest (400: not gzip / JSON / an object), TooLarge (413), Invalid
(422, the first problem named). Standard library only
(tests/test_ingest_validate.py).
"""

import json
import math
import re
import uuid
import zlib
from datetime import datetime, timezone

MAX_BODY = 2 * 1024 * 1024        # compressed request body (Caddy: request_body max_size 2MB)
MAX_JSON = 16 * 1024 * 1024       # inflated
MAX_POLLS = 240
MAX_ROWS = 30000
MIN_TS = 1788220800               # 2026-09-01T00:00:00Z: nothing of this project is older
FUTURE_S = 300

INT4 = (-2 ** 31, 2 ** 31 - 1)
INT8 = (-2 ** 63, 2 ** 63 - 1)
INT2 = (-2 ** 15, 2 ** 15 - 1)
_MAC = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")
_USB_ID = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")


class BadRequest(Exception):
    pass


class TooLarge(Exception):
    pass


class Invalid(Exception):
    pass


# (column, kind, required); kinds: int / bigint / small (integers of that
# range), flag (0/1), real, text (+ max length), mac, ts (unix seconds,
# integer, a data time), tsf (the same, fractional), tstz (unix seconds ->
# datetime for a timestamptz stage column), json (a JSON text)
_CONFIG = [("ssid", "text", False), ("cloaked", "flag", False), ("crypt", "text", False),
           ("crypt_bits", "bigint", False), ("mfp_sup", "flag", False), ("mfp_req", "flag", False),
           ("adv_channel", "text", False), ("ht_mode", "text", False), ("beacon_rate", "int", False),
           ("country", "text", False)]

TABLES = {
    "devices": [("device_key", "text", True), ("mac", "mac", True), ("type", "text", True),
                ("manuf", "text", False), ("first_seen", "ts", True), ("last_seen", "ts", True)]
               + _CONFIG + [("config_changed_at", "ts", False), ("cur_n_clients", "int", False),
                            ("cur_qbss_stations", "int", False), ("cur_util_pct", "real", False),
                            ("cur_at", "ts", False)],
    "polls": [("seq", "bigint", True), ("ts", "ts", True), ("ts_offset_s", "real", False),
              ("kismet_ts", "ts", False), ("devices_total", "int", False), ("devices_active", "int", False),
              ("new_obs", "int", False), ("ds_running", "flag", False), ("ds_error", "text", False),
              ("ds_packets", "bigint", False), ("ds_hop_n", "int", False), ("ds_hop_visited", "int", False),
              ("ds_hop_ok", "flag", False), ("duration_ms", "int", False), ("ok", "flag", True),
              ("error", "text", False), ("config_version", "int", False)],
    "device_config_history": [("device_key", "text", True), ("ts", "ts", True)] + _CONFIG,
    "observations": [("ts", "ts", True), ("device_key", "text", True), ("rssi", "small", False),
                     ("rssi_floor", "flag", False), ("pk_total", "bigint", False), ("pk_data", "bigint", False),
                     ("disconnects", "int", False), ("disconnects_last", "ts", False),
                     ("bss_timestamp", "bigint", False)],
    "ap_load_hourly": [("device_key", "text", True), ("hour", "ts", True), ("channel", "text", True),
                       ("polls", "int", True), ("clients_polls", "int", True), ("clients_max", "int", False),
                       ("clients_sum", "int", False), ("qbss_polls", "int", True), ("qbss_max", "int", False),
                       ("qbss_sum", "int", False), ("util_max", "real", False), ("util_sum", "real", False)],
    "load_samples": [("ts", "tstz", True), ("device_key", "text", True), ("n_clients", "int", False),
                     ("qbss_stations", "int", False), ("util_pct", "real", False)],
    "device_freq_hist": [("device_key", "text", True), ("freq_khz", "int", True), ("packets", "bigint", True),
                         ("updated_ts", "ts", True)],
    "alerts": [("hash", "text", True), ("ts", "tsf", False), ("header", "text", True), ("class", "text", False),
               ("severity", "small", False), ("source_mac", "mac", False), ("dest_mac", "mac", False),
               ("transmitter_mac", "mac", False), ("other_mac", "mac", False), ("channel", "text", False),
               ("freq_khz", "int", False), ("device_key", "text", False), ("text", "text", False),
               ("raw", "json", True)],
}
# the merge order (foreign keys first), also the COPY order
ORDER = ("devices", "polls", "device_config_history", "observations", "ap_load_hourly", "load_samples",
         "device_freq_hist", "alerts")
KEYS = {"devices": ("device_key",), "polls": ("ts",), "device_config_history": ("device_key", "ts"),
        "observations": ("device_key", "ts"), "ap_load_hourly": ("device_key", "hour", "channel"),
        "load_samples": ("device_key", "ts"), "device_freq_hist": ("device_key", "freq_khz"),
        "alerts": ("hash",)}
# columns an older uploader does not send yet: cols may end before them (the
# value is then NULL) - polls.config_version from uploader 1.2 / buffer v9
OPTIONAL_TAIL = {"polls": ("config_version",)}
TEXT_MAX = {"ds_error": 500, "error": 500, "text": 4000, "raw": 65536}
TEXT_DEFAULT_MAX = 256


def columns(table):
    return [c[0] for c in TABLES[table]]


# --- body ------------------------------------------------------------------------------------

def decode_body(raw, content_encoding):
    """The JSON object of a request body (gzip or identity), with size limits."""
    if len(raw) > MAX_BODY:
        raise TooLarge("body larger than %d bytes" % MAX_BODY)
    enc = (content_encoding or "identity").strip().lower()
    if enc == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            data = d.decompress(raw, MAX_JSON + 1)
        except zlib.error as e:
            raise BadRequest("body is not gzip: %s" % e) from None
        if len(data) > MAX_JSON or d.unconsumed_tail:
            raise TooLarge("inflated body larger than %d bytes" % MAX_JSON)
        if not d.eof:
            raise BadRequest("truncated gzip body")
    elif enc == "identity":
        data = raw
    else:
        raise BadRequest("unsupported Content-Encoding %r" % enc)
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError) as e:
        raise BadRequest("body is not JSON: %s" % e) from None
    if not isinstance(obj, dict):
        raise BadRequest("body is not a JSON object")
    return obj


# --- values --------------------------------------------------------------------------------------

def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _num(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _value(where, col, kind, required, x, now):
    """The value as it goes into the stage; Invalid when it does not fit."""
    if x is None:
        if required:
            raise Invalid("%s: %s is required" % (where, col))
        return None
    if kind in ("int", "bigint", "small"):
        lo, hi = {"int": INT4, "bigint": INT8, "small": INT2}[kind]
        if not _is_int(x) or not lo <= x <= hi:
            raise Invalid("%s: %s must be an integer in %d..%d" % (where, col, lo, hi))
        return x
    if kind == "flag":
        if isinstance(x, bool):
            return int(x)
        if x not in (0, 1) or not _is_int(x):
            raise Invalid("%s: %s must be 0 or 1" % (where, col))
        return x
    if kind == "real":
        if not _num(x):
            raise Invalid("%s: %s must be a finite number" % (where, col))
        return x
    if kind in ("ts", "tsf", "tstz"):
        if (not _is_int(x)) if kind == "ts" else (not _num(x)):
            raise Invalid("%s: %s must be unix seconds" % (where, col))
        if x < MIN_TS:
            raise Invalid("%s: %s is before 2026-09-01" % (where, col))
        if x > now + FUTURE_S:
            raise Invalid("%s: %s is in the future (the sensor's clock?)" % (where, col))
        return datetime.fromtimestamp(x, timezone.utc) if kind == "tstz" else x
    if kind == "mac":
        if not isinstance(x, str) or not _MAC.fullmatch(x):
            raise Invalid("%s: %s must be a MAC address" % (where, col))
        return x
    if kind in ("text", "json"):
        if not isinstance(x, str) or "\x00" in x:
            raise Invalid("%s: %s must be text without NUL" % (where, col))
        if len(x) > TEXT_MAX.get(col, TEXT_DEFAULT_MAX):
            raise Invalid("%s: %s longer than %d characters" % (where, col, TEXT_MAX.get(col, TEXT_DEFAULT_MAX)))
        if kind == "json":
            try:
                json.loads(x)
            except ValueError:
                raise Invalid("%s: %s is not JSON" % (where, col)) from None
        return x
    raise AssertionError(kind)


def _ap_evidence(row):
    """schema.sql ap_evidence(): typed ap, or an advertised configuration."""
    return row["type"] == "ap" or any(row[c] is not None for c in ("ssid", "crypt", "adv_channel"))


def _table(name, t, now):
    if not isinstance(t, dict) or not isinstance(t.get("rows"), list):
        raise Invalid("%s: {cols, rows} expected" % name)
    spec = TABLES[name]
    full = [c[0] for c in spec]
    tail = OPTIONAL_TAIL.get(name, ())
    short = full[:len(full) - len(tail)] if tail and full[len(full) - len(tail):] == list(tail) else None
    if t.get("cols") == full:
        pad = 0
    elif short is not None and t.get("cols") == short:
        pad = len(tail)
    else:
        raise Invalid("%s: columns must be %s" % (name, ", ".join(full)))
    out, seen = [], set()
    key_idx = [i for i, c in enumerate(spec) if c[0] in KEYS[name]]
    for n, row in enumerate(t["rows"], 1):
        where = "%s row %d" % (name, n)
        if not isinstance(row, list) or len(row) != len(spec) - pad:
            raise Invalid("%s: %d values expected" % (where, len(spec) - pad))
        row = row + [None] * pad
        vals = [_value(where, c[0], c[1], c[2], x, now) for c, x in zip(spec, row)]
        key = tuple(vals[i] for i in key_idx)
        if key in seen:
            raise Invalid("%s: duplicate key %s" % (where, ", ".join(KEYS[name])))
        seen.add(key)
        if name == "devices" and not _ap_evidence(dict(zip([c[0] for c in spec], vals))):
            raise Invalid("%s: not an AP (a sensor never uploads a client device)" % where)
        out.append(vals)
    return out


# --- envelope ------------------------------------------------------------------------------------

def _uuid(x, what):
    try:
        return str(uuid.UUID(x)) if isinstance(x, str) else _bad(what)
    except ValueError:
        return _bad(what)


def _bad(what):
    raise Invalid("%s is invalid" % what)


def _boots(items):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 16:
        raise Invalid("boots: a list of at most 16 expected")
    out = []
    for b in items:
        if not isinstance(b, dict) or b.get("source") not in ("ntp", "server") or not _num(b.get("epoch_true")):
            raise Invalid("boots: boot_id, source ntp|server and epoch_true are required")
        row = {"boot_id": _uuid(b.get("boot_id"), "boots: boot_id"), "source": b["source"],
               "epoch_true": float(b["epoch_true"])}
        for k in ("epoch_stale", "step_mono", "offset_min_s", "offset_max_s"):
            x = b.get(k)
            if x is not None and not _num(x):
                raise Invalid("boots: %s must be a number" % k)
            row[k] = None if x is None else float(x)
        pc = b.get("polls_corrected", 0)
        if not _is_int(pc) or pc < 0:
            raise Invalid("boots: polls_corrected must be a count")
        row["polls_corrected"] = pc
        out.append(row)
    return out


def _gaps(items):
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 100:
        raise Invalid("gaps: a list of at most 100 expected")
    out = []
    for g in items:
        if not isinstance(g, dict):
            raise Invalid("gaps: objects expected")
        a, b, reason = g.get("seq_from"), g.get("seq_to"), g.get("reason")
        if not (_is_int(a) and _is_int(b) and 1 <= a <= b) or reason not in ("lost", "unverified"):
            raise Invalid("gaps: seq_from <= seq_to and reason lost|unverified required")
        row = {"seq_from": a, "seq_to": b, "reason": reason, "polls": b - a + 1}
        for k in ("raw_from", "raw_to"):
            x = g.get(k)
            if x is not None and not _num(x):
                raise Invalid("gaps: %s must be unix seconds" % k)
            row[k] = None if x is None else datetime.fromtimestamp(x, timezone.utc)
        out.append(row)
    return out


def batch(body, now):
    """The validated batch: envelope fields, {table: [rows]} in ORDER, the
    span of its polls, the row count, heartbeat / boots / gaps."""
    if body.get("v") != 1:
        raise Invalid("unsupported payload version %r" % body.get("v"))
    seq, attempt = body.get("batch_seq"), body.get("attempt", 1)
    if not _is_int(seq) or seq < 0:
        raise Invalid("batch_seq must be an integer >= 0")
    if not _is_int(attempt) or attempt < 1:
        raise Invalid("attempt must be an integer >= 1")
    version = body.get("buffer_version")
    if not isinstance(version, str) or not 0 < len(version) <= 16:
        raise Invalid("buffer_version is invalid")
    sent_at = body.get("sent_at")
    if sent_at is not None and not _num(sent_at):
        raise Invalid("sent_at must be unix seconds")
    tables = body.get("tables")
    if not isinstance(tables, dict):
        raise Invalid("tables must be an object")
    unknown = sorted(set(tables) - set(TABLES))
    if unknown:
        raise Invalid("unknown table(s): %s (the live ingest never takes client tables)" % ", ".join(unknown))
    out, rows = {}, 0
    for name in ORDER:
        if name in tables:
            out[name] = _table(name, tables[name], now)
            rows += len(out[name])
    polls = out.get("polls", [])
    if len(polls) > MAX_POLLS:
        raise Invalid("polls: at most %d per batch" % MAX_POLLS)
    if rows > MAX_ROWS:
        raise Invalid("at most %d rows per batch" % MAX_ROWS)
    ts_i = columns("polls").index("ts")
    hb = body.get("heartbeat")
    return {
        "batch_seq": seq, "attempt": attempt, "buffer_id": _uuid(body.get("buffer_id"), "buffer_id"),
        "buffer_version": version,
        "sent_at": None if sent_at is None else datetime.fromtimestamp(sent_at, timezone.utc),
        "tables": out, "rows": rows,
        "polls_from": min((p[ts_i] for p in polls), default=None),
        "polls_to": max((p[ts_i] for p in polls), default=None),
        "heartbeat": None if hb is None else heartbeat(hb),
        "boots": _boots(body.get("boots")), "gaps": _gaps(body.get("gaps")),
    }


# --- heartbeat --------------------------------------------------------------------------------

_HB = {"sensor_ts": "num", "mono": "num", "boot_id": "uuid", "uptime_s": "num", "rtt_ms": "int",
       "chrony_synced": "bool", "chrony_offset_s": "num", "chrony_stratum": "int", "last_poll_id": "int",
       "last_poll_ts": "num", "last_poll_ok": "bool", "buffer_bytes": "int", "buffer_version": "text",
       "buffer_id": "uuid", "retention_days": "int", "buffer_oldest_ts": "num", "backlog_polls": "int",
       "backlog_oldest_ts": "num", "held_polls": "int", "unverified_polls": "int", "disk_free_bytes": "int",
       "uploader_version": "text", "last_result": "text"}
_ADAPTER = ("usb_id", "mac", "driver", "chipset", "interface")


def heartbeat(hb):
    """The known fields of a heartbeat (unknown ones are dropped - a newer
    uploader may send more); sensor_ts is required."""
    if not isinstance(hb, dict):
        raise Invalid("heartbeat must be an object")
    out = {}
    for k, kind in _HB.items():
        x = hb.get(k)
        if x is None:
            out[k] = None
            continue
        ok = {"num": _num(x), "int": _is_int(x), "bool": isinstance(x, bool),
              "text": isinstance(x, str) and len(x) <= 200 and "\x00" not in x,
              "uuid": isinstance(x, str)}[kind]
        if not ok:
            raise Invalid("heartbeat: %s is invalid" % k)
        out[k] = _uuid(x, "heartbeat: " + k) if kind == "uuid" else x
    if out["sensor_ts"] is None:
        raise Invalid("heartbeat: sensor_ts is required")
    a = hb.get("adapter")
    if a is None:
        out["adapter"] = None
    elif not isinstance(a, dict):
        raise Invalid("heartbeat: adapter must be an object or null")
    else:
        ad = {}
        for k in _ADAPTER:
            x = a.get(k)
            if x is not None and (not isinstance(x, str) or len(x) > 64 or "\x00" in x):
                raise Invalid("heartbeat: adapter.%s is invalid" % k)
            ad[k] = x
        if ad["usb_id"] is not None and not _USB_ID.fullmatch(ad["usb_id"]):
            raise Invalid("heartbeat: adapter.usb_id must be vid:pid in lower-case hex")
        if ad["mac"] is not None and not _MAC.fullmatch(ad["mac"]):
            raise Invalid("heartbeat: adapter.mac must be a MAC address")
        out["adapter"] = ad
    # configuration delivery (schema 18): uploader 1.2 reports these; older ones do not
    from . import config as _config
    out["config_state"] = _config.parse_state(hb.get("config_state"))
    out["capabilities"] = _config.parse_capabilities(hb.get("capabilities"))
    return out
