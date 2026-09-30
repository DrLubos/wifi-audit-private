"""Shared pieces of the read endpoints: sensor resolution, validation, caching."""

from fastapi import HTTPException, Query, Request, Response

from . import cache, db

# Kismet device keys look like 4202770D00000000_E4AAEA5F5CDD.
DEVICE_KEY_RE = r"^[0-9A-Fa-f_]{1,64}$"


def get_sensor(sensor: str | None = Query(
        None, description="sensors.name to read; default: the first sensor")):
    """Resolve the sensor every endpoint is scoped to (one today, many by design).

    refreshed_at: when refresh_rollups() last built the sensor's dashboard
    tables (NULL before the first run) - the version behind the ETags.
    """
    sql = ("SELECT s.id, s.name, s.location, s.tz, ss.refreshed_at "
           "FROM sensors s LEFT JOIN sensor_summary ss ON ss.sensor_id = s.id ")
    with db.pool.connection() as conn:
        if sensor is None:
            row = conn.execute(sql + "ORDER BY s.id LIMIT 1").fetchone()
        else:
            row = conn.execute(sql + "WHERE s.name = %s", (sensor,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="unknown sensor" if sensor else "no sensor seeded yet")
    return row


def require_rollups(sensor):
    """The dashboard tables exist only after refresh_rollups() ran for the sensor."""
    if sensor["refreshed_at"] is None:
        raise HTTPException(
            status_code=503,
            detail="dashboard tables not built yet: SELECT * FROM refresh_rollups(%d)" % sensor["id"])


def rollup_etag(sensor, *extra):
    return cache.rollup_etag(sensor["id"], sensor["refreshed_at"], *extra)


def not_modified(request: Request, etag):
    """A 304 response when the client's copy (If-None-Match) is current, else None."""
    if cache.matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=cache.headers(etag))
    return None


def set_cache_headers(response: Response, etag):
    response.headers.update(cache.headers(etag))


def epoch(dt):
    """timestamptz -> unix seconds (int) for compact time series."""
    return int(dt.timestamp()) if dt is not None else None
