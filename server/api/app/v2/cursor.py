"""Opaque keyset cursors of the event feed: (ts, id) <-> a URL-safe token.

The feed is ordered by (ts DESC, id DESC) and paged with `before` / `after`
cursors, never OFFSET (PLAN.md 7.3). A token is base64url("<ts in us>.<id>")
without padding; decode() accepts only the canonical encoding of a valid pair
and raises ValueError otherwise (the api answers 422).

Standard library only (tests/test_cursor.py).
"""

import base64
import binascii
import re
from datetime import datetime, timezone

_PAIR = re.compile(r"^(-?[0-9]{1,17})\.([0-9]{1,19})$")
_MAX_ID = 2 ** 63 - 1


def encode(ts, id_):
    us = round(ts.timestamp() * 1_000_000) if ts.tzinfo else None
    if us is None:
        raise ValueError("cursor ts must be timezone-aware")
    raw = ("%d.%d" % (us, id_)).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(token):
    """-> (ts as an aware UTC datetime, id); ValueError for anything else."""
    if not isinstance(token, str) or not 1 <= len(token) <= 64:
        raise ValueError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("invalid cursor") from None
    m = _PAIR.match(raw)
    if m is None:
        raise ValueError("invalid cursor")
    us, id_ = int(m.group(1)), int(m.group(2))
    if id_ < 1 or id_ > _MAX_ID:
        raise ValueError("invalid cursor")
    try:
        ts = datetime.fromtimestamp(us // 1_000_000, timezone.utc).replace(microsecond=us % 1_000_000)
    except (OverflowError, OSError, ValueError):
        raise ValueError("invalid cursor") from None
    if encode(ts, id_) != token:        # only the canonical form (no padding, no leading zeros)
        raise ValueError("invalid cursor")
    return ts, id_
