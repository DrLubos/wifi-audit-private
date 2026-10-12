"""Validation of the v2 write bodies (PLAN.md 7.5): each parse_* takes the
JSON body and returns the normalized values the write SQL stores, or raises
Invalid (a 422 with its message). Also the channel plans of Settings >
Sensors and Kismet 2025-09's hop-stride rule, ported from
wifi-sensor/install_sensor.sh (hop_stride_ok).

Standard library only (tests/test_writes.py).
"""

import math
import re
from datetime import datetime, timedelta, timezone


class Invalid(ValueError):
    """A body the endpoint refuses (422)."""


# --- event labels -------------------------------------------------------------------------------

# the fixed reasons of a label (schema 11, detections_status_reason): Confirm =
# the event's condition was real, False positive = it was not
REASONS = {
    "confirmed": ("security_relevant", "expected_benign", "staged_test"),
    "false_positive": ("rule_defect", "artefact"),
}
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
    """{status, reason?, note?} -> (status, reason, note). Open clears the
    reason; confirmed / false_positive need one of their reasons."""
    status = body.get("status")
    if status not in ("open", "confirmed", "false_positive"):
        raise Invalid("status: open, confirmed or false_positive")
    reason = body.get("reason")
    if status == "open":
        if reason is not None:
            raise Invalid("reason: none for open")
    elif reason not in REASONS[status]:
        raise Invalid("reason for %s: %s" % (status, ", ".join(REASONS[status])))
    return status, reason, _note(body.get("note"))


def parse_ids(body):
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > BULK_MAX:
        raise Invalid("ids: 1 to %d event ids" % BULK_MAX)
    if not all(isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 63 for i in ids):
        raise Invalid("ids: positive integers")
    return sorted(set(ids))


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

def parse_sensor_meta(body):
    def text(key, limit):
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or len(v.strip()) > limit:
            raise Invalid("%s: at most %d characters" % (key, limit))
        return v.strip() or None
    return {"location": text("location", 120), "description": text("description", 500)}


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
PLANS = ("all_89", "ht20_37", "locked", "custom")             # no_empty_dfs_59: not offered (PLAN 5)
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


# --- capture hardware ---------------------------------------------------------------------------

BANDS = ("2.4", "5", "6")


def parse_hardware(body, kind_fixed=None):
    """A catalog row -> {kind, name, gain_dbi, bands, connector, note}.
    kind_fixed: an update keeps the stored kind."""
    kind = kind_fixed or body.get("kind")
    if kind not in ("adapter", "antenna"):
        raise Invalid("kind: adapter or antenna")
    name = body.get("name")
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 80:
        raise Invalid("name: 1 to 80 characters")
    gain = body.get("gain_dbi")
    if gain is not None and (not isinstance(gain, (int, float)) or isinstance(gain, bool) or not -10 <= gain <= 30):
        raise Invalid("gain_dbi: a number -10 to 30, or null")
    bands = body.get("bands") or []
    if not isinstance(bands, list) or not bands or not set(bands) <= set(BANDS):
        raise Invalid("bands: one or more of " + ", ".join(BANDS))
    connector = body.get("connector")
    if connector is not None and (not isinstance(connector, str) or len(connector.strip()) > 40):
        raise Invalid("connector: at most 40 characters")
    return {"kind": kind, "name": name.strip(), "gain_dbi": None if gain is None else float(gain),
            "bands": [b for b in BANDS if b in bands], "connector": (connector or "").strip() or None,
            "note": _note(body.get("note"), 500)}


def parse_attach(body, now, last_from=None):
    """'Attached since': {adapter_id, antenna_id?, valid_from?, note?} ->
    {adapter_id, antenna_id, valid_from, note}. valid_from defaults to now,
    may lie in the past (a swap recorded later) but after the sensor's last
    record, and not in the future (5 min of clock skew allowed)."""
    def ident(key, allow_none):
        v = body.get(key)
        if v is None and allow_none:
            return None
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise Invalid("%s: a catalog id" % key)
        return v
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
    if last_from is not None and t <= last_from:
        raise Invalid("valid_from: after the sensor's last hardware change (%s)"
                      % last_from.astimezone(timezone.utc).isoformat())
    return {"adapter_id": ident("adapter_id", False), "antenna_id": ident("antenna_id", True),
            "valid_from": t, "note": _note(body.get("note"), 500)}
