"""GET /api/overview - capture span, poll health, device counts, alert count."""

from fastapi import APIRouter, Depends

from .. import db
from ..deps import get_sensor

router = APIRouter(prefix="/api", tags=["overview"])

_GAPS = """
    SELECT prev_ts AS gap_start, ts AS gap_end, extract(epoch FROM gap)::float8 AS gap_s
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts,
                 ts - lag(ts) OVER (ORDER BY ts) AS gap
          FROM polls WHERE sensor_id = %s) g
    WHERE gap > interval '90 seconds'
    ORDER BY gap DESC LIMIT 10"""

_GAP_TOTALS = """
    SELECT count(*) AS n,
           coalesce(extract(epoch FROM sum(gap)), 0)::float8 AS total_s,
           coalesce(extract(epoch FROM max(gap)), 0)::float8 AS max_s
    FROM (SELECT ts - lag(ts) OVER (ORDER BY ts) AS gap
          FROM polls WHERE sensor_id = %s) g
    WHERE gap > interval '90 seconds'"""


@router.get("/overview")
def overview(sensor=Depends(get_sensor)):
    sid = sensor["id"]
    with db.pool.connection() as conn:
        polls = conn.execute(
            "SELECT min(ts) AS first_poll, max(ts) AS last_poll, count(*) AS total, "
            "count(*) FILTER (WHERE ok) AS ok FROM polls WHERE sensor_id = %s", (sid,)).fetchone()
        gaps = conn.execute(_GAPS, (sid,)).fetchall()
        gap_totals = conn.execute(_GAP_TOTALS, (sid,)).fetchone()
        types = conn.execute(
            "SELECT type, count(*) AS n FROM devices WHERE sensor_id = %s GROUP BY type",
            (sid,)).fetchall()
        # polls.new_obs is the number of observation rows the collector wrote
        # in that poll, so the sum equals count(*) FROM observations (verified
        # 1 082 443 = 1 082 443 on the seeded data) at 14k rows instead of a
        # 1 M-row scan that took 36 s on a cold cache.
        obs = conn.execute(
            "SELECT coalesce(sum(new_obs), 0)::bigint AS n FROM polls WHERE sensor_id = %s",
            (sid,)).fetchone()
        alerts = conn.execute(
            "SELECT count(*) AS total, max(ts) AS last_ts FROM alerts WHERE sensor_id = %s",
            (sid,)).fetchone()
        baselines = conn.execute(
            "SELECT count(*) AS n FROM ap_baselines WHERE sensor_id = %s", (sid,)).fetchone()

    by_type = {r["type"]: r["n"] for r in types}
    main_types = ("ap", "client", "bridged")
    devices = {t: by_type.get(t, 0) for t in main_types}
    devices["other"] = sum(n for t, n in by_type.items() if t not in main_types)
    devices["total"] = sum(by_type.values())

    total, ok = polls["total"], polls["ok"]
    span_s = ((polls["last_poll"] - polls["first_poll"]).total_seconds()
              if polls["first_poll"] else 0)
    return {
        "sensor": {"name": sensor["name"], "location": sensor["location"], "tz": sensor["tz"]},
        "capture": {"first_poll": polls["first_poll"], "last_poll": polls["last_poll"],
                    "span_s": span_s},
        "polls": {
            "total": total, "ok": ok,
            "coverage_pct": round(ok * 100.0 / total, 2) if total else None,
            "gaps": {"count": gap_totals["n"], "total_s": gap_totals["total_s"],
                     "max_s": gap_totals["max_s"], "list": gaps},
        },
        "devices": devices,
        "observations": obs["n"],
        "alerts": {"total": alerts["total"], "last_ts": alerts["last_ts"]},
        "baselines": baselines["n"],
    }
