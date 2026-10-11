"""The v2 privacy rule as a check: no MAC in a response other than an AP BSSID.

SPEC section 2: client devices only ever appear as counts - no client MAC,
probe SSID or per-client row in the api. macs_in() finds every MAC-shaped
string in a decoded JSON response (also the MAC inside a Kismet device key,
<16 hex>_<12 hex>); foreign_macs() returns those that are not in the set of
the account's AP BSSIDs. Used by tests/test_privacy.py on sample responses
and by the smoke test on real responses (tests/perf/v2_smoke.py).

Standard library only.
"""

import re

_MAC = re.compile(r"(?<![0-9A-Fa-f:])([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})(?![0-9A-Fa-f:])")
_KEY = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16}_([0-9A-Fa-f]{12})(?![0-9A-Fa-f])")


def normalize(mac):
    """'aa-bb-cc-dd-ee-ff' / 'AABBCCDDEEFF' -> 'AA:BB:CC:DD:EE:FF'."""
    h = re.sub(r"[^0-9A-Fa-f]", "", mac).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


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
        found.update(normalize(m) for m in _KEY.findall(s))
    return found


def foreign_macs(obj, bssids):
    """MACs in obj that are not AP BSSIDs (bssids: any spelling)."""
    allowed = {normalize(b) for b in bssids}
    return macs_in(obj) - allowed
