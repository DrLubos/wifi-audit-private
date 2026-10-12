"""The write token (PLAN.md 7.5): until login exists (11.4) every write
endpoint needs `Authorization: Bearer <WRITE_TOKEN>`, the token from the
server's .env that compose passes to the api. An empty WRITE_TOKEN disables
writes (403); a missing or wrong token is a 401. Compared in constant time.

Standard library only (tests/test_write_token.py).
"""

import hmac

OK, MISSING, WRONG, DISABLED = "ok", "missing", "wrong", "disabled"
MIN_LENGTH = 16          # a shorter configured token is treated as not set (a typo, not a secret)


def bearer(header):
    """The token of an `Authorization: Bearer <token>` header, else None."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def check(header, configured):
    """OK, MISSING, WRONG or DISABLED for an Authorization header against
    the configured token."""
    configured = (configured or "").strip()
    if len(configured) < MIN_LENGTH:
        return DISABLED
    token = bearer(header)
    if token is None:
        return MISSING
    if not hmac.compare_digest(token.encode("utf-8"), configured.encode("utf-8")):
        return WRONG
    return OK
