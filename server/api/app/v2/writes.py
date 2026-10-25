"""Validation of the v2 write bodies (PLAN.md 7.5): each parse_* takes the
JSON body and returns the normalized values the write SQL stores, or raises
Invalid (a 422 with its message). Also the channel plans of Settings >
Sensors and Kismet 2025-09's hop-stride rule, ported from
wifi-sensor/install_sensor.sh (hop_stride_ok).

Standard library only (tests/test_writes.py).
"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone


class Invalid(ValueError):
    """A body the endpoint refuses (422)."""


# --- event labels -------------------------------------------------------------------------------

# the three outcomes of a label and their required reasons (schema 12,
# detections_status_reason; RQ2 splits by them): confirmed = a real security
# event, benign = the condition was real but no threat (e.g. NOCLIENTMFP on an
# open network), false_positive = no real condition or a misattribution
REASONS = {
    "confirmed": ("security_relevant", "staged_test"),
    "benign": ("expected_behaviour", "accepted_risk"),
    "false_positive": ("rule_defect", "artefact"),
}
STATUSES = ("open",) + tuple(REASONS)
# the label's cause (schema 17, RQ2): why a benign or false-positive event
# fired - the same list as schema.sql detections_status_cause (tested) and
# frontend events.js CAUSES (tests/test_schema_files.py). "unknown" says it
# was looked at and no cause is known; "other" needs a note naming it.
CAUSES = (
    ("rssi_floor", "RSSI floor censoring (the -106/-120 dBm sentinel read as a level)"),
    ("channel_hopping", "Channel hopping / reduced channel coverage"),
    ("kismet_rule", "Kismet rule defect, or a rule that describes no security event (e.g. NOCLIENTMFP)"),
    ("allow_list_incomplete", "Incomplete allow-list (BSSIDs / OUIs of the network)"),
    ("pipeline_bug", "A bug in this project's own pipeline (e.g. event 866)"),
    ("sensor_degraded", "Capture down or no frames at the time"),
    ("ap_behaviour", "Normal AP behaviour (channel selection, DFS, reboot, owner's change)"),
    ("client_behaviour", "Normal client behaviour (roaming, leaving, power save)"),
    ("staged_test", "This project's own staged test (spill-over)"),
    ("duplicate", "Repeats an incident already reported (a grouping artefact)"),
    ("unknown", "No known cause"),
    ("other", "Other (named in the note)"),
)
CAUSE_KEYS = tuple(k for k, _ in CAUSES)
WITH_CAUSE = ("benign", "false_positive")
NOTE_MAX = 1000
BULK_MAX = 500


def _note(v, limit=NOTE_MAX):
    if v is None:
        return None
    if not isinstance(v, str):
        raise Invalid("note: a string")
    v = v.strip()
    if len(v) > limit:
        raise Invalid("note: at most %d characters" % limit)
    return v or None


def parse_status(body):
    """{status, reason?, cause?, note?} -> (status, reason, cause, note). Open
    clears the reason; each resolved outcome needs one of its reasons; benign
    and false_positive need a cause (schema 17), confirmed and open have none;
    the cause "other" needs a note."""
    status = body.get("status")
    if status not in STATUSES:
        raise Invalid("status: " + ", ".join(STATUSES))
    reason = body.get("reason")
    if status == "open":
        if reason is not None:
            raise Invalid("reason: none for open")
    elif reason not in REASONS[status]:
        raise Invalid("reason for %s: %s" % (status, ", ".join(REASONS[status])))
    cause = body.get("cause")
    note = _note(body.get("note"))
    if status in WITH_CAUSE:
        if cause not in CAUSE_KEYS:
            raise Invalid("cause for %s: %s" % (status, ", ".join(CAUSE_KEYS)))
        if cause == "other" and note is None:
            raise Invalid("note: required for the cause other (name the cause)")
    elif cause is not None:
        raise Invalid("cause: only for benign and false_positive")
    return status, reason, cause, note


def parse_ids(body):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > BULK_MAX:
        raise Invalid("ids: 1 to %d event ids" % BULK_MAX)
    if not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 63 for i in ids):
        raise Invalid("ids: positive integers")
    return sorted(set(ids))


# --- AP classification and note ------------------------------------------------------------------

CLASSES = ("managed", "friendly", "unknown", "suspicious", "ignored")


def parse_ap_classification(body):
    """{classification} -> a class, or None = back to the rules."""
    if "classification" not in body:
        raise Invalid("classification: a class or null")
    c = body["classification"]
    if c is not None and c not in CLASSES:
        raise Invalid("classification: " + ", ".join(CLASSES) + " or null")
    return c


def parse_ap_note(body):
    if "note" not in body:
        raise Invalid("note: a string or null")
    return _note(body["note"], 2000)


# --- networks -----------------------------------------------------------------------------------

EXPECTED = ("enterprise", "psk_or_better", "wpa3", "open_allowed")
HEX = re.compile(r"^[0-9A-Fa-f]{2}([:-]?[0-9A-Fa-f]{2})*$")
ALLOWED_MAX = 200


def _octets(v, n, what):
    if not isinstance(v, str) or not HEX.match(v.strip()):
        raise Invalid("%s: %r is not a MAC address" % (what, v))
    digits = re.sub(r"[:-]", "", v.strip()).upper()
    if len(digits) != 2 * n:
        raise Invalid("%s: %r needs %d octets" % (what, v, n))
    return ":".join(digits[i:i + 2] for i in range(0, 2 * n, 2))


def bssid(v):
    """'ec-58-ea-55-88-fc' -> 'EC:58:EA:55:88:FC'."""
    return _octets(v, 6, "bssid")


def oui(v):
    """'ec58ea' -> 'EC:58:EA'."""
    return _octets(v, 3, "oui")


def parse_network(body):
    """A managed / friendly network -> {ssid, kind, expected_security,
    require_mfp, allowed_bssids, allowed_ouis, note}. A friendly network is
    not checked: no expected security, no MFP requirement (the table's
    CHECK)."""
    ssid = body.get("ssid")
    if not isinstance(ssid, str) or not ssid.strip() or len(ssid.encode("utf-8")) > 32:
        raise Invalid("ssid: 1 to 32 bytes")
    kind = body.get("kind")
    if kind not in ("managed", "friendly"):
        raise Invalid("kind: managed or friendly")
    expected = body.get("expected_security")
    require_mfp = body.get("require_mfp", False)
    if not isinstance(require_mfp, bool):
        raise Invalid("require_mfp: true or false")
    if kind == "managed":
        if expected is not None and expected not in EXPECTED:
            raise Invalid("expected_security: " + ", ".join(EXPECTED))
    else:
        expected, require_mfp = None, False
    bssids = body.get("allowed_bssids") or []
    ouis = body.get("allowed_ouis") or []
    if not isinstance(bssids, list) or not isinstance(ouis, list) or len(bssids) + len(ouis) > ALLOWED_MAX:
        raise Invalid("allowed_bssids / allowed_ouis: lists, together at most %d" % ALLOWED_MAX)
    return {
        "ssid": ssid,                                                # an SSID's spaces are its own
        "kind": kind, "expected_security": expected, "require_mfp": require_mfp,
        "allowed_bssids": sorted({bssid(b) for b in bssids}),
        "allowed_ouis": sorted({oui(o) for o in ouis}),
        "note": _note(body.get("note"), 500),
    }


def parse_networks(body):
    """The Networks tab saves the whole list: [{id?, ...network}] - an id
    updates that network, none adds one, a stored id left out is removed."""
    rows = body.get("networks")
    if not isinstance(rows, list) or len(rows) > 500:
        raise Invalid("networks: a list (at most 500)")
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            raise Invalid("networks: objects")
        n = parse_network(r)
        nid = r.get("id")
        if nid is not None and (not isinstance(nid, int) or isinstance(nid, bool) or nid <= 0):
            raise Invalid("id: a positive integer")
        if n["ssid"] in seen:
            raise Invalid("ssid %r twice" % n["ssid"])
        seen.add(n["ssid"])
        out.append(dict(n, id=nid))
    return out


def parse_allowed(body):
    """'Add to allowed': {bssid} or {oui} -> ('bssid' | 'oui', value)."""
    if ("bssid" in body) == ("oui" in body):
        raise Invalid("either bssid or oui")
    return ("bssid", bssid(body["bssid"])) if "bssid" in body else ("oui", oui(body["oui"]))


# --- account settings ---------------------------------------------------------------------------

def _bool(body, key):
    v = body.get(key)
    if not isinstance(v, bool):
        raise Invalid("%s: true or false" % key)
    return v


def _int(body, key, lo, hi, allow_none=False):
    v = body.get(key)
    if v is None and allow_none:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise Invalid("%s: a whole number %d to %d%s" % (key, lo, hi, " or null" if allow_none else ""))
    return v


def _num(body, key, lo, hi):
    v = body.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise Invalid("%s: a number %g to %g" % (key, lo, hi))
    return float(v)


def parse_classification(body):
    vendors = body.get("mobile_vendors")
    if (not isinstance(vendors, list) or len(vendors) > 30
            or not all(isinstance(v, str) and 0 < len(v.strip()) <= 40 for v in vendors)):
        raise Invalid("mobile_vendors: at most 30 names of 1 to 40 characters")
    return {
        "ignore_random_bssid": _bool(body, "ignore_random_bssid"),
        "suspicious_unallowed": _bool(body, "suspicious_unallowed"),
        "managed_by_vendor": _bool(body, "managed_by_vendor"),
        "ignore_weaker_dbm": _int(body, "ignore_weaker_dbm", -120, -30, allow_none=True),
        "mobile_vendors": list(dict.fromkeys(v.strip() for v in vendors)),
    }


GUARDS = (0, 600, 1800, 3600)        # the board's choices: off, 10 min, 30 min, 1 h


def parse_detectors(body):
    out = {
        "deauth_min_bursts": _int(body, "deauth_min_bursts", 1, 100),
        "deauth_window_s": _int(body, "deauth_window_s", 30, 86400),
        "evil_twin_k": _num(body, "evil_twin_k", 1, 50),
        "evil_twin_min_db": _num(body, "evil_twin_min_db", 1, 60),
        "evil_twin_guard_s": _int(body, "evil_twin_guard_s", 0, 3600),
        "sensor_offline_min": _int(body, "sensor_offline_min", 1, 1440),
    }
    if out["evil_twin_guard_s"] not in GUARDS:
        raise Invalid("evil_twin_guard_s: one of %s" % ", ".join(map(str, GUARDS)))
    return out


# --- sensors ------------------------------------------------------------------------------------

SENSOR_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def sensor_name(v):
    """A sensor's name: a short handle (it is in URLs and in the importer's
    --sensor option), 1 to 40 of letters, digits, . _ -"""
    if not isinstance(v, str) or not SENSOR_NAME.match(v.strip()):
        raise Invalid("name: 1 to 40 characters - letters, digits, . _ - (it is used in URLs and by the importer)")
    return v.strip()


def parse_sensor_meta(body, need_name=False):
    """{name?, location?, description?} -> the fields to store. The name is
    required for a new sensor, optional for an edit (absent = unchanged; a
    rename is the same write - sensor_id is the key, the name a unique label)."""
    def text(key, limit):
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or len(v.strip()) > limit:
            raise Invalid("%s: at most %d characters" % (key, limit))
        return v.strip() or None
    out = {"location": text("location", 120), "description": text("description", 500)}
    if need_name or body.get("name") is not None:
        out["name"] = sensor_name(body.get("name"))
    return out


def hop_stride(n):
    """(stride, gcd) Kismet 2025-09's capture helper hops a list of n entries
    with: it prefers 4 (capture_linux_wifi.c) and keeps the first s >= 4 with
    n % (n // s) != 0 (capture_framework.c) - not a coprimality test, so only
    n / gcd(n, s) entries are visited. Same search as install_sensor.sh."""
    s = 4
    if s > n:
        s = 1
    while n % (n // s) == 0:
        if s >= n - 1:
            s = 1
            break
        s += 1
    return s, math.gcd(n, s)


def stride_ok(n):
    return hop_stride(n)[1] == 1


# The 89-entry list of wifi-sensor/install_sensor.sh (RTW88_8821CU_CHANNELS;
# tests/test_writes.py compares the two): Kismet's autodetected list for
# rtw88_8821cu without the invalid 40 MHz pairings 165HT40- and 140HT40-.
ALL_89 = (
    "1,1HT40+,2,3,4,5,6,6HT40-,6HT40+,7,8,9,10,11,11HT40-,12,13,"
    "36,36HT40+,36VHT80,40,40HT40-,40VHT80,44,44HT40+,44VHT80,48,48HT40-,48VHT80,"
    "52,52HT40+,52VHT80,56,56HT40-,56VHT80,60,60HT40+,60VHT80,64,64HT40-,64VHT80,"
    "100,100HT40+,100VHT80,104,104HT40-,104VHT80,108,108HT40+,108VHT80,112,112HT40-,112VHT80,"
    "116,116HT40+,116VHT80,120,120HT40-,120VHT80,124,124HT40+,124VHT80,128,128HT40-,128VHT80,"
    "132,132HT40+,132VHT80,136,136HT40-,136VHT80,140,140VHT80,144,144HT40-,144VHT80,"
    "149,149HT40+,149VHT80,153,153HT40-,153VHT80,157,157HT40+,157VHT80,161,161HT40-,161VHT80,165"
).split(",")
HT20_ALL = tuple(c for c in ALL_89 if c.isdigit())          # 38: a composite length (stride 4, gcd 2)
# 20 MHz only, 37 entries (prime): the 38 without 144 - no AP of the
# account's data has used it (ap_load_hourly, 2026-10-12) and it is outside
# ETSI's 5470-5725 MHz band
HT20_37 = tuple(c for c in HT20_ALL if c != "144")
ENTRY = re.compile(r"^(\d{1,3})(HT40[+-]|VHT80|VHT160)?$")
PLANS = ("all_89", "ht20_37", "locked", "custom")             # no_empty_dfs_59: dropped (schema 12)
DWELLS = (0.2, 0.5, 1.0)
POLLS = (15, 30, 60)


def plan_channels(plan, channels=None, lock=None):
    """The hop list of a plan: all_89, ht20_37, locked (one 20 MHz channel
    of the 89 list) or custom (Kismet entries, each once, in the given
    order). Every list must satisfy the stride rule."""
    if plan == "all_89":
        out = list(ALL_89)
    elif plan == "ht20_37":
        out = list(HT20_37)
    elif plan == "locked":
        if lock not in HT20_ALL:
            raise Invalid("lock_channel: a 20 MHz channel of the 89 list (1-13, 36-165)")
        out = [lock]
    elif plan == "custom":
        if not isinstance(channels, list) or not 1 <= len(channels) <= 200:
            raise Invalid("channels: 1 to 200 entries")
        out = []
        for c in channels:
            c = c.strip().upper() if isinstance(c, str) else c
            if not isinstance(c, str) or not ENTRY.match(c):
                raise Invalid("channels: %r is not a Kismet entry (e.g. 6, 6HT40+, 36VHT80)" % (c,))
            if c in out:
                raise Invalid("channels: %s twice" % c)
            out.append(c)
    else:
        raise Invalid("channel_plan: " + ", ".join(PLANS))
    stride, gcd = hop_stride(len(out))
    if gcd != 1:
        raise Invalid("channels: %d entries - Kismet hops them with stride %d and would visit only %d "
                      "(the length must be coprime with the stride; a prime length always is)"
                      % (len(out), stride, len(out) // gcd))
    return out


# no 802.11 40 MHz partner - Kismet's autodetected list has both (findings.md
# 7); the same set as wifi-sensor/sensor/capture_config.py KNOWN_INVALID
KNOWN_INVALID = frozenset({"165HT40-", "140HT40-"})


def check_capture_config(channels, dwell_s, poll_s, capabilities=None):
    """The delivery rules (docs/config-delivery.md 4.1) - the same verdicts as
    the Pi's capture_config.validate() on the shared fixture: Kismet entries,
    each once, none of KNOWN_INVALID, each in the sensor's reported
    channels_supported (when it reported them), the stride rule, the dwell
    and poll bounds. Raises Invalid."""
    if not isinstance(channels, list) or not 1 <= len(channels) <= 200:
        raise Invalid("channels: 1 to 200 entries")
    seen = []
    for c in channels:
        if not isinstance(c, str) or not ENTRY.match(c.strip().upper()):
            raise Invalid("channels: %r is not a Kismet entry" % (c,))
        c = c.strip().upper()
        if c in seen:
            raise Invalid("channels: %s twice" % c)
        if c in KNOWN_INVALID:
            raise Invalid("channels: %s is not a valid 40 MHz pairing" % c)
        seen.append(c)
    supported = (capabilities or {}).get("channels_supported")
    if supported:
        sup = {x.strip().upper() for x in supported if isinstance(x, str)}
        missing = [c for c in seen if c not in sup]
        if missing:
            raise Invalid("channels: %s not supported by the adapter" % ", ".join(missing[:5]))
    stride, gcd = hop_stride(len(seen))
    if gcd != 1:
        raise Invalid("channels: %d entries - Kismet hops them with stride %d and would visit only %d "
                      "(the length must be coprime with the stride)" % (len(seen), stride, len(seen) // gcd))
    if isinstance(dwell_s, bool) or not isinstance(dwell_s, (int, float)) or float(dwell_s) not in DWELLS:
        raise Invalid("dwell_s: one of %s" % ", ".join(map(str, DWELLS)))
    if isinstance(poll_s, bool) or not isinstance(poll_s, int) or poll_s not in POLLS:
        raise Invalid("poll_s: one of %s" % ", ".join(map(str, POLLS)))
    return seen


def config_canonical(cfg):
    """The canonical JSON of a configuration (the Pi's capture_config.canonical)."""
    return json.dumps({"v": 1, "channels": [c.strip().upper() for c in cfg["channels"]],
                       "dwell_s": round(float(cfg["dwell_s"]), 3), "poll_s": cfg["poll_s"]},
                      sort_keys=True, separators=(",", ":")).encode("ascii")


def config_sha256(cfg):
    return hashlib.sha256(config_canonical(cfg)).hexdigest()


def parse_capture_config(body):
    """{sensors: [names], config: {channel_plan, channels?, lock_channel?,
    dwell_s, poll_s}, note?} -> (names, config)."""
    names = body.get("sensors")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise Invalid("sensors: one or more sensor names")
    c = body.get("config")
    if not isinstance(c, dict):
        raise Invalid("config: an object")
    channels = plan_channels(c.get("channel_plan"), c.get("channels"), c.get("lock_channel"))
    dwell = c.get("dwell_s")
    if not isinstance(dwell, (int, float)) or isinstance(dwell, bool) or float(dwell) not in DWELLS:
        raise Invalid("dwell_s: one of %s" % ", ".join(map(str, DWELLS)))
    poll = c.get("poll_s")
    if poll not in POLLS or isinstance(poll, bool):
        raise Invalid("poll_s: one of %s" % ", ".join(map(str, POLLS)))
    return sorted(set(names)), {"channel_plan": c["channel_plan"], "channels": channels,
                                "n_channels": len(channels), "dwell_s": float(dwell), "poll_s": poll,
                                "note": _note(body.get("note"), 500)}


# --- hardware change ----------------------------------------------------------------------------

def parse_hardware_change(body, now):
    """{valid_from?, note?} -> {valid_from, note}: a hardware change entered by
    hand (the antenna swapped, the sensor moved - anything that shifts every
    RSSI); valid_from defaults to now, may lie in the past (recorded later), not
    in the future (5 min of clock skew allowed). It is a data boundary of the
    history - RSSI baselines never span it."""
    at = body.get("valid_from")
    if at is None:
        t = now
    else:
        try:
            t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except ValueError:
            raise Invalid("valid_from: an ISO time") from None
        if t.tzinfo is None:
            raise Invalid("valid_from: with a time zone (e.g. 2026-10-12T18:00:00Z)")
    if t > now + timedelta(minutes=5):
        raise Invalid("valid_from: not in the future")
    t = t.astimezone(timezone.utc).replace(microsecond=0)
    return {"valid_from": t, "note": _note(body.get("note"), 500)}


def hardware_change_label(note):
    """The history label of a hardware change."""
    return "Hardware change" + (": " + note if note else "")
