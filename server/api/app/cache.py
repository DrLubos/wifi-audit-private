"""HTTP caching of the read endpoints: ETag and Cache-Control.

The dashboard tables (schema 6: ap_summary, ap_rssi_hourly, sensor_summary,
ap_config_changes) change only when refresh_rollups() runs, and every run
stamps sensor_summary.refreshed_at. So the ETag of an endpoint that reads only
those tables is derived from that stamp (plus whatever else the response
shows, e.g. the live detection counts of the overview): the api answers a
matching If-None-Match with 304 before running any query. Weak ETags, so
Caddy's compression (encode zstd gzip) leaves them alone.

Standard library only (unit-tested without FastAPI: tests/test_cache.py).
"""

# Same freshness as the other read endpoints; after that the browser
# revalidates and gets a 304 until the next refresh.
CACHE_CONTROL = "public, max-age=60"


def rollup_etag(sensor_id, refreshed_at, *extra):
    """W/"<sensor>-<refreshed_at in us>[-<extra>...]", None before the first refresh.

    extra: integers (or None) that the response also depends on.
    """
    if refreshed_at is None:
        return None
    parts = [str(int(sensor_id)), str(round(refreshed_at.timestamp() * 1_000_000))]
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
