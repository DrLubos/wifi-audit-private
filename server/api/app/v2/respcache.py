"""In-process cache of v2 response bodies, keyed by URL + ETag.

The 30-day aggregates cost 0.1-0.3 s of the e2-micro's CPU warm and 1-2 s
cold, but their data changes only when an import refreshes the rollups or a
write bumps accounts.write_version, and their buckets once a day - all of
which is in the v2 ETag. So a body computed once is served again from
memory until the tag of its URL moves; a stale entry is never hit again and
leaves through the LRU. Bounded by size (V2_CACHE_BYTES, default 4 MiB;
bodies over a quarter of it are not kept). One uvicorn worker; a lock for
its thread pool.

A period response holds the request time (period.to and its label), which
must not be served from an entry a day old: the body is kept without its
period envelope (the keys "filters" and "period") and every hit gets the
envelope of the current request.

Standard library only (tests/test_respcache.py).
"""

import json
import os
import threading
from collections import OrderedDict
from datetime import date, datetime
from decimal import Decimal

from .periods import iso

ENVELOPE_KEYS = ("filters", "period")
MAX_BYTES = int(os.environ.get("V2_CACHE_BYTES", str(4 * 1024 * 1024)))


class Tag(str):
    """An ETag that also names the cache entry of its request and carries the
    request's fresh period envelope (None: the body has none)."""
    key = None
    envelope = None


def make_tag(value, key, envelope=None):
    if value is None:
        return None
    t = Tag(value)
    t.key, t.envelope = key, envelope
    return t


def request_key(path, query_items):
    """The URL with its query parameters sorted (?a=1&b=2 == ?b=2&a=1)."""
    q = "&".join("%s=%s" % kv for kv in sorted(query_items))
    return path + ("?" + q if q else "")


def _default(v):
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    raise TypeError("not JSON serializable: %r" % type(v))


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=_default).encode("utf-8")


def join(envelope, rest):
    """The JSON object of envelope's keys followed by those of rest (bytes of an object)."""
    if not envelope:
        return rest
    head = dumps(envelope)
    if rest == b"{}":
        return head
    return head[:-1] + b"," + rest[1:]


class LRU:
    def __init__(self, max_bytes=MAX_BYTES):
        self.max_bytes = max_bytes
        self.bytes = 0
        self._items = OrderedDict()           # (key, etag) -> bytes
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key, etag):
        with self._lock:
            body = self._items.get((key, etag))
            if body is None:
                self.misses += 1
                return None
            self._items.move_to_end((key, etag))
            self.hits += 1
            return body

    def put(self, key, etag, body):
        if len(body) > self.max_bytes // 4:
            return False
        with self._lock:
            old = self._items.pop((key, etag), None)
            if old is not None:
                self.bytes -= len(old)
            self._items[(key, etag)] = body
            self.bytes += len(body)
            while self.bytes > self.max_bytes and self._items:
                _, dropped = self._items.popitem(last=False)
                self.bytes -= len(dropped)
        return True

    def __len__(self):
        return len(self._items)

    def clear(self):
        with self._lock:
            self._items.clear()
            self.bytes = 0


cache = LRU()


def lookup(tag):
    """The cached body for this request (with its fresh envelope), or None."""
    if not isinstance(tag, Tag) or tag.key is None:
        return None
    rest = cache.get(tag.key, str(tag))
    return None if rest is None else join(tag.envelope, rest)


def render(body, tag):
    """Serialize a response body and keep it for the next request with the same tag."""
    if not isinstance(tag, Tag) or tag.key is None:
        return dumps(body)
    if tag.envelope is not None:
        env = {k: body[k] for k in ENVELOPE_KEYS if k in body}
        rest = dumps({k: v for k, v in body.items() if k not in ENVELOPE_KEYS})
        cache.put(tag.key, str(tag), rest)
        return join(env, rest)
    out = dumps(body)
    cache.put(tag.key, str(tag), out)
    return out
