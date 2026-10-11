"""The v2 privacy rule as a check: no MAC in a response other than an AP BSSID.

SPEC section 2: client devices only ever appear as counts - no client MAC,
probe SSID or per-client row in the api. macs_in() finds every MAC-shaped
string in a decoded JSON response (also the MAC inside a Kismet device key,
<16 hex>_<12 hex>, which holds it byte-reversed: ..._DC8955EA58EC is
EC:58:EA:55:89:DC); foreign_macs() returns those that are not in the set of
the account's AP BSSIDs; redact() replaces those in a value before it is
returned (the detectors' evidence names deauth sources, i.e. client MACs).
Used by the events endpoints, tests/test_privacy.py and the smoke test on
real responses (tests/perf/v2_smoke.py).

Standard library only.
"""

import re

_MAC = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})(?![0-9A-Fa-f:])")
_KEY = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16}_([0-9A-Fa-f]{12})(?![0-9A-Fa-f])")


def normalize(mac):
    """'aa-bb-cc-dd-ee-ff' / 'AABBCCDDEEFF' -> 'AA:BB:CC:DD:EE:FF'."""
    h = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def key_mac(hex12):
    """The MAC of a device key's last part (byte-reversed)."""
    h = hex12.upper()
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
