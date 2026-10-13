"""HTTP caching of the read endpoints: ETag and Cache-Control.

The dashboard tables (ap_summary, ap_rssi_hourly, sensor_summary,
ap_config_changes) change only when refresh_rollups() runs, and every run
stamps sensor_summary.refreshed_at. So the ETag of an endpoint is derived from
that stamp (plus the account's write version and the newest event change): the
api answers a matching If-None-Match with 304 before running any query. Weak
ETags, so Caddy's compression (encode zstd gzip) leaves them alone.

Standard library only (unit-tested without FastAPI: tests/test_cache.py).
"""

# Same freshness as the other read endpoints; after that the browser
# revalidates and gets a 304 until the next refresh.
CACHE_CONTROL = "public, max-age=60"


def v2_etag(account_id, refreshed_at, write_version, events_at, bucket_epoch, *extra):
    """The tag of a v2 endpoint (PLAN.md 7.1), None before the first refresh:

    W/"v2-<account>-<max refreshed_at of its sensors, us>-<write_version>-
       <max detections.updated_at, us>-<bucket epoch>[-<extra>...]"

    bucket_epoch: the start of the current bucket for period endpoints (a new
    hour / 6-hour window / day changes the boundaries), the current minute for
    status endpoints, None where time does not matter.
    """
    if refreshed_at is None:
        return None
    us = (lambda t: "x" if t is None else str(round(t.timestamp() * 1_000_000)))
    parts = ["v2", str(int(account_id)), us(refreshed_at), str(int(write_version)), us(events_at),
             "x" if bucket_epoch is None else str(int(bucket_epoch))]
    parts += ["x" if v is None else str(int(v)) for v in extra]
    return 'W/"%s"' % "-".join(parts)


def _opaque(tag):
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def matches(if_none_match, etag):
    """Weak comparison (RFC 9110 13.1.2) of an If-None-Match header with etag."""
    if not if_none_match or not etag:
        return False
    if if_none_match.strip() == "*":
        return True
    want = _opaque(etag)
    return any(_opaque(t) == want for t in if_none_match.split(","))


def headers(etag):
    h = {"Cache-Control": CACHE_CONTROL}
    if etag:
        h["ETag"] = etag
    return h
