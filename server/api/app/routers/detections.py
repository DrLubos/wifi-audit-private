"""GET /api/detections, /api/detections/{id} - server-side detections (detection/).

Read-only: rows are written by the batch detectors and acknowledged elsewhere
(no ack endpoint yet). The list carries no evidence (it is most of a row's
size); GET /api/detections/{id} returns one row with it, loaded when a row is
expanded. Filters narrow the list; the summary counters are always over the
whole sensor so the page header does not move with the filters.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .. import db
from ..deps import get_sensor

router = APIRouter(prefix="/api", tags=["detections"])

SEVERITIES = ("info", "low", "medium", "high", "critical")

# The AP the row is about: by device_key, or by BSSID for a row without a
# device row (an attacker spoofing a BSSID Kismet never classified).
_SELECT = """
    SELECT d.id, d.ts, d.type, d.severity, d.device_key, upper(d.mac::text) AS mac, d.ssid,
           d.summary, d.acked, d.acked_at, d.ack_note, d.created_at,
           ap.device_key AS ap_device_key, ap.ssid AS ap_ssid,
           upper(ap.bssid::text) AS ap_bssid, ap.hidden AS ap_hidden, ap.trusted AS ap_trusted{extra}
    FROM detections d
    LEFT JOIN LATERAL (
      SELECT device_key, ssid, bssid, hidden, trusted, last_seen
      FROM ap_summary i
      WHERE i.sensor_id = d.sensor_id
        AND (i.device_key = d.device_key OR (d.device_key IS NULL AND i.bssid = d.mac))
      ORDER BY last_seen DESC LIMIT 1) ap ON true"""

_LIST = _SELECT.format(extra="") + """
    WHERE d.sensor_id = %(sid)s
      AND (%(severity)s::text    IS NULL OR d.severity = %(severity)s)
      AND (%(type)s::text        IS NULL OR d.type = %(type)s)
      AND (%(acked)s::boolean    IS NULL OR d.acked = %(acked)s)
    ORDER BY d.ts DESC, d.id DESC
    LIMIT %(limit)s"""

_ONE = _SELECT.format(extra=", d.evidence") + """
    WHERE d.sensor_id = %s AND d.id = %s"""

_SUMMARY = """
    SELECT severity, type, count(*) AS n, count(*) FILTER (WHERE NOT acked) AS open
    FROM detections WHERE sensor_id = %s GROUP BY severity, type"""


@router.get("/detections")
def list_detections(
        severity: str | None = Query(None, pattern="^(%s)$" % "|".join(SEVERITIES)),
        type: str | None = Query(None, max_length=64),
        acked: bool | None = Query(None, description="true = acknowledged only, false = open only"),
        limit: int = Query(200, ge=1, le=1000),
        sensor=Depends(get_sensor)):
    sid = sensor["id"]
    with db.pool.connection() as conn:
        summary = conn.execute(_SUMMARY, (sid,)).fetchall()
        rows = conn.execute(_LIST, {"sid": sid, "severity": severity, "type": type,
                                    "acked": acked, "limit": limit}).fetchall()
    by_severity = {s: 0 for s in SEVERITIES}
    by_type = {}
    for r in summary:
        by_severity[r["severity"]] = by_severity.get(r["severity"], 0) + r["n"]
        by_type[r["type"]] = by_type.get(r["type"], 0) + r["n"]
    return {
        "total": sum(r["n"] for r in summary),
        "open": sum(r["open"] for r in summary),
        "by_severity": by_severity,
        "by_type": by_type,
        "count": len(rows),
        "detections": rows,
    }


@router.get("/detections/{detection_id}")
def get_detection(detection_id: int = Path(ge=1), sensor=Depends(get_sensor)):
    """One detection with its evidence (the list leaves the evidence out)."""
    with db.pool.connection() as conn:
        row = conn.execute(_ONE, (sensor["id"], detection_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown detection")
    return row
