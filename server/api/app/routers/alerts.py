"""GET /api/alerts - Kismet's native WIDS alerts, newest first."""

from fastapi import APIRouter, Depends, Query

from .. import db
from ..deps import get_sensor

router = APIRouter(prefix="/api", tags=["alerts"])


@router.get("/alerts")
def list_alerts(limit: int = Query(100, ge=1, le=1000), sensor=Depends(get_sensor)):
    sid = sensor["id"]
    with db.pool.connection() as conn:
        by_header = conn.execute(
            "SELECT header, count(*) AS n FROM alerts WHERE sensor_id = %s "
            "GROUP BY header ORDER BY n DESC, header", (sid,)).fetchall()
        rows = conn.execute(
            "SELECT a.id, a.ts, a.header, a.class, a.severity, "
            "upper(a.source_mac::text) AS source_mac, upper(a.dest_mac::text) AS dest_mac, "
            "upper(a.transmitter_mac::text) AS transmitter_mac, a.channel, a.freq_khz, "
            "a.device_key, d.ssid AS device_ssid, d.type AS device_type, a.text "
            "FROM alerts a LEFT JOIN devices d "
            "  ON d.sensor_id = a.sensor_id AND d.device_key = a.device_key "
            "WHERE a.sensor_id = %s ORDER BY a.ts DESC NULLS LAST, a.id DESC LIMIT %s",
            (sid, limit)).fetchall()
    return {
        "total": sum(r["n"] for r in by_header),
        "by_header": {r["header"]: r["n"] for r in by_header},
        "alerts": rows,
    }
