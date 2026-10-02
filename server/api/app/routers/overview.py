"""GET /api/overview - capture span, poll health, device counts, alert and detection counts.

Schema 6: one row of sensor_summary (built by refresh_rollups() at import
time) instead of scans over polls and devices; only the detection counts are
read live, since the batch detectors write detections outside an import. The
ETag covers both.
"""

from fastapi import APIRouter, Depends, Request, Response

from .. import db
from ..deps import epoch, get_sensor, not_modified, require_rollups, rollup_etag, set_cache_headers

router = APIRouter(prefix="/api", tags=["overview"])

_DETECTIONS = """
    SELECT count(*) AS total, count(*) FILTER (WHERE NOT acked) AS open, max(ts) AS last_ts
    FROM detections WHERE sensor_id = %s"""


@router.get("/overview")
def overview(request: Request, response: Response, sensor=Depends(get_sensor)):
    require_rollups(sensor)
    sid = sensor["id"]
    with db.pool.connection() as conn:
        detections = conn.execute(_DETECTIONS, (sid,)).fetchone()
        etag = rollup_etag(sensor, detections["total"], detections["open"],
                           epoch(detections["last_ts"]))
        cached = not_modified(request, etag)
        if cached:
            return cached
        s = conn.execute("SELECT * FROM sensor_summary WHERE sensor_id = %s", (sid,)).fetchone()
    set_cache_headers(response, etag)

    by_type = s["devices_by_type"]
    main_types = ("ap", "client", "bridged")
    devices = {t: by_type.get(t, 0) for t in main_types}
    devices["other"] = sum(n for t, n in by_type.items() if t not in main_types)
    devices["total"] = sum(by_type.values())

    total, ok = s["polls"], s["polls_ok"]
    span_s = ((s["last_poll"] - s["first_poll"]).total_seconds() if s["first_poll"] else 0)
    return {
        "sensor": {"name": sensor["name"], "location": sensor["location"], "tz": sensor["tz"]},
        "capture": {"first_poll": s["first_poll"], "last_poll": s["last_poll"], "span_s": span_s},
        "polls": {
            "total": total, "ok": ok,
            "coverage_pct": round(ok * 100.0 / total, 2) if total else None,
            "gaps": {"count": s["gap_count"], "total_s": s["gap_total_s"],
                     "max_s": s["gap_max_s"], "list": s["gaps"]},
        },
        "devices": devices,
        # schema 7: the AP observation rows the server keeps (sum of
        # sensor_hourly.ap_obs); the collector's rows of every device type
        # (sum of polls.new_obs) stay on the Pi
        "observations": s["obs_ap"],
        "collector_rows": s["obs_total"],
        "alerts": {"total": s["alerts"], "last_ts": s["alerts_last_ts"]},
        "baselines": s["baselines"],
        "detections": {"total": detections["total"], "open": detections["open"],
                       "last_ts": detections["last_ts"]},
        "refreshed_at": s["refreshed_at"],
    }
