"""The sensor's capture configuration: the rules a desired configuration
must pass before it is applied (server/docs/config-delivery.md 4.1) and its
canonical form and hash. Standard library only; used by config_apply.py and
the uploader. The server has the same rules in api/app/v2/writes.py; the
fixture tests/capture_config_cases.json keeps the two equal."""

import hashlib
import json
import math
import re

ENTRY = re.compile(r"^(\d{1,3})(HT40[+-]|VHT80|VHT160)?$")
# no 802.11 40 MHz partner: 165HT40- caused the rtw88 WARN storm, 136+140 is
# not a 40 MHz channel (findings.md 7) - Kismet's autodetected list has both
KNOWN_INVALID = frozenset({"165HT40-", "140HT40-"})
DWELLS = (0.2, 0.5, 1.0)
POLLS = (15, 30, 60)
MAX_ENTRIES = 200


def hop_stride(n):
    """Kismet 2025-09's hop stride for a list of n entries and gcd(n, stride)
    (capture_framework.c: the first s >= 4 with n % (n // s) != 0; the
    installer's hop_stride_ok). Every entry is visited only when gcd == 1."""
    s = 4
    if s > n:
        s = 1
    while n % (n // s) == 0:
        if s >= n - 1:
            s = 1
            break
        s += 1
    return s, math.gcd(n, s)


def normalise(cfg):
    """Upper-case entries, float dwell; the input is not modified."""
    # dwell rounded: the server stores it as a real (0.2 reads back as 0.20000000298)
    return {"channels": [c.strip().upper() for c in cfg["channels"]], "dwell_s": round(float(cfg["dwell_s"]), 3),
            "poll_s": cfg["poll_s"]}


def canonical(cfg):
    """The canonical JSON bytes whose sha256 identifies a configuration."""
    c = normalise(cfg)
    return json.dumps({"v": 1, "channels": c["channels"], "dwell_s": c["dwell_s"], "poll_s": c["poll_s"]},
                      sort_keys=True, separators=(",", ":")).encode("ascii")


def sha256(cfg):
    return hashlib.sha256(canonical(cfg)).hexdigest()


def validate(cfg, capabilities=None):
    """(ok, reason): the rules of config-delivery.md 4.1. capabilities:
    {"channels_supported": [...]} as the sensor reports it, or None (then
    only the static rules apply)."""
    if not isinstance(cfg, dict):
        return False, "configuration: an object"
    chans = cfg.get("channels")
    if not isinstance(chans, list) or not 1 <= len(chans) <= MAX_ENTRIES:
        return False, "channels: 1 to %d entries" % MAX_ENTRIES
    seen = []
    for c in chans:
        if not isinstance(c, str) or not ENTRY.match(c.strip().upper()):
            return False, "channels: %r is not a Kismet entry" % (c,)
        c = c.strip().upper()
        if c in seen:
            return False, "channels: %s twice" % c
        if c in KNOWN_INVALID:
            return False, "channels: %s is not a valid 40 MHz pairing" % c
        seen.append(c)
    supported = (capabilities or {}).get("channels_supported")
    if supported:
        sup = {s.strip().upper() for s in supported if isinstance(s, str)}
        missing = [c for c in seen if c not in sup]
        if missing:
            return False, "channels: %s not supported by the adapter" % ", ".join(missing[:5])
    stride, gcd = hop_stride(len(seen))
    if gcd != 1:
        return False, ("channels: %d entries - Kismet hops them with stride %d and would visit only %d "
                       "(the length must be coprime with the stride)" % (len(seen), stride, len(seen) // gcd))
    dwell = cfg.get("dwell_s")
    if isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or float(dwell) not in DWELLS:
        return False, "dwell_s: one of %s" % ", ".join(map(str, DWELLS))
    poll = cfg.get("poll_s")
    if isinstance(poll, bool) or not isinstance(poll, int) or poll not in POLLS:
        return False, "poll_s: one of %s" % ", ".join(map(str, POLLS))
    return True, None
