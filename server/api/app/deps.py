"""Shared pieces of the read endpoints: sensor resolution and validation."""

from fastapi import HTTPException, Query

from . import db

# Kismet device keys look like 4202770D00000000_E4AAEA5F5CDD.
DEVICE_KEY_RE = r"^[0-9A-Fa-f_]{1,64}$"


def get_sensor(sensor: str | None = Query(
        None, description="sensors.name to read; default: the first sensor")):
    """Resolve the sensor every endpoint is scoped to (one today, many by design)."""
    with db.pool.connection() as conn:
        if sensor is None:
            row = conn.execute(
                "SELECT id, name, location, tz FROM sensors ORDER BY id LIMIT 1").fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, location, tz FROM sensors WHERE name = %s", (sensor,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="unknown sensor" if sensor else "no sensor seeded yet")
    return row


def epoch(dt):
    """timestamptz -> unix seconds (int) for compact time series."""
    return int(dt.timestamp()) if dt is not None else None
