"""The v2 privacy rule as a check: no MAC in a response other than an AP BSSID.

SPEC section 2: client devices only ever appear as counts - no client MAC,
probe SSID or per-client row in the api. macs_in() finds every MAC-shaped
string in a decoded JSON response (also the MAC inside a Kismet device key,
<16 hex>_<up to 12 hex>, which holds it byte-reversed: ..._DC8955EA58EC is
EC:58:EA:55:89:DC); foreign_macs() returns those that are not in the set of
the account's AP BSSIDs; redact() replaces those in a value before it is
returned (the detectors' evidence names deauth sources, i.e. client MACs).
Used by the events endpoints, tests/test_privacy.py and the smoke test on
real responses (tests/perf/v2_smoke.py).

Standard library only.
"""

import re

# Six octets that are not part of a longer run of octets. A neighbouring
# colon alone does not make a run: "from 3C:22:FB:01:02:03: 20 frames" and
# "src:3C:22:FB:01:02:03" hold a MAC (the first version of this pattern
# refused any MAC next to a colon and let both through). A run is another
# whole octet on either side - "AA:BB:CC:DD:EE:FF:00", or "ab:" before the
# six when "ab" stands alone (after a non-alphanumeric character or at the
# start; the "ac:" of "mac:" is the end of a word, not an octet).
_HEX = "[0-9A-Fa-f]"
_MAC = re.compile(r"(?<!%(h)s)(?<![^0-9A-Za-z]%(h)s{2}[:-])(?<!^%(h)s{2}[:-])"
                  r"(%(h)s{2}(?:[:-]%(h)s{2}){5})"
                  r"(?!%(h)s)(?![:-]%(h)s{2}(?!%(h)s))" % {"h": _HEX})
# The key's second part is the MAC as a number in hex, WITHOUT leading zeros
# (Kismet prints %X): EC:58:EA:55:89:0C is ..._C8955EA58EC, eleven digits. 6 %
# of the stored keys are shorter than twelve (13 960 of 222 843 on 2026-11-15)
# and the first version of this pattern, twelve digits exactly, missed them.
_KEY = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16}_([0-9A-Fa-f]{1,12})(?![0-9A-Fa-f])")


def normalize(mac):
    """'aa-bb-cc-dd-ee-ff' / 'AABBCCDDEEFF' -> 'AA:BB:CC:DD:EE:FF'."""
    h = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def key_mac(hex12):
    """The MAC of a device key's last part (byte-reversed; leading zeros restored)."""
    h = hex12.upper().zfill(12)
    return ":".join(h[i:i + 2] for i in range(10, -2, -2))


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def macs_in(obj):
    found = set()
    for s in _strings(obj):
        found.update(normalize(m) for m in _MAC.findall(s))
        found.update(key_mac(m) for m in _KEY.findall(s))
    return found


def foreign_macs(obj, bssids):
    """MACs in obj that are not AP BSSIDs (bssids: any spelling)."""
    allowed = {normalize(b) for b in bssids}
    return macs_in(obj) - allowed


MASK = "(redacted)"


def redact(obj, bssids):
    """A copy of obj in which every MAC that is not in bssids (also inside a
    device key or a longer text, also in dict keys) is replaced by MASK."""
    allowed = {normalize(b) for b in bssids}

    def text(s):
        s = _MAC.sub(lambda m: m.group(0) if normalize(m.group(1)) in allowed else MASK, s)
        return _KEY.sub(lambda m: m.group(0) if key_mac(m.group(1)) in allowed else MASK, s)

    def walk(v):
        if isinstance(v, str):
            return text(v)
        if isinstance(v, dict):
            return {text(k) if isinstance(k, str) else k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        return v
    return walk(obj)
