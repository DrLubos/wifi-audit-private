"""Per-sensor ingest tokens (docs/live-upload.md 6.1).

A token is `wsi1.<id>.<64 hex>`: the id is public - it names the
sensor_tokens row, so a wrong secret can still be attributed to its sensor
(the upload_unauthorized stall event) - and the server stores only the
SHA-256 of the whole token (ops/sensor_token.sh: `printf %s "$token" |
sha256sum`). SHA-256, not a slow hash: 256 random bits need none, and the
box has a quarter of a core. Never log a token: describe() gives its id.

Standard library only (tests/test_ingest_tokens.py).
"""

import hashlib
import hmac
import re

PREFIX = "wsi1"
_TOKEN = re.compile(r"wsi1\.([1-9][0-9]{0,9})\.([0-9a-f]{64})")


def make(token_id, secret_hex):
    return "%s.%d.%s" % (PREFIX, token_id, secret_hex)


def parse(token):
    """The token's id, or None for anything that is not a well-formed token."""
    if not isinstance(token, str):
        return None
    m = _TOKEN.fullmatch(token)
    return int(m.group(1)) if m else None


def digest(token):
    return hashlib.sha256(token.encode("ascii")).digest()


def matches(token, stored_hash):
    """Constant-time comparison of the token's hash with the stored one."""
    return hmac.compare_digest(digest(token), bytes(stored_hash or b""))


def from_header(authorization):
    """The token of an `Authorization: Bearer <token>` header, else None."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def describe(token):
    """What a log line may say about a token."""
    token_id = parse(token)
    return "token %d" % token_id if token_id is not None else "a malformed token"
