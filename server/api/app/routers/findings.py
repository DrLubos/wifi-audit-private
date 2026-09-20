"""GET /api/findings - the RSSI-stability distribution across ap_baselines.

The headline numbers of docs/findings.md section 2, computed from the stored
baselines (median + MAD-robust sigma per AP, >= 200 readings on >= 2 days).
The threshold sweep and the day-to-day drift stay offline analyses: they need
every reading, not a baseline row.
"""

from fastapi import APIRouter, Depends

from .. import db
from ..deps import get_sensor

router = APIRouter(prefix="/api", tags=["findings"])

THRESHOLDS_DB = (2, 3, 5, 8)
BASELINE_MIN_OBS = 200      # defaults of refresh_ap_baselines() / the seed
BASELINE_MIN_DAYS = 2

_within = ", ".join(
    "count(*) FILTER (WHERE rssi_sd <= %d) AS sd_le_%d, "
    "count(*) FILTER (WHERE rssi_robust_sd <= %d) AS rsd_le_%d" % (t, t, t, t)
    for t in THRESHOLDS_DB)

_SUMMARY = """
    SELECT count(*) AS aps, sum(n_obs) AS readings, max(computed_at) AS computed_at,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY rssi_sd) AS sd_median,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY rssi_sd) AS sd_q3,
           percentile_cont(0.9)  WITHIN GROUP (ORDER BY rssi_sd) AS sd_p90,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY rssi_robust_sd) AS rsd_median,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY rssi_robust_sd) AS rsd_q3,
           percentile_cont(0.9)  WITHIN GROUP (ORDER BY rssi_robust_sd) AS rsd_p90,
           """ + _within + """
    FROM ap_baselines WHERE sensor_id = %s"""

_BY_BAND = """
    SELECT CASE WHEN main_freq_khz < 3000000 THEN '2.4'
                WHEN main_freq_khz < 5925000 THEN '5' ELSE '6' END AS band,
           count(*) AS aps,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_sd) AS sd_median,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_robust_sd) AS rsd_median,
           count(*) FILTER (WHERE rssi_sd <= 3) AS sd_le_3,
           count(*) FILTER (WHERE rssi_sd <= 5) AS sd_le_5
    FROM ap_baselines WHERE sensor_id = %s AND main_freq_khz IS NOT NULL
    GROUP BY 1 ORDER BY 1"""


@router.get("/findings")
def findings(sensor=Depends(get_sensor)):
    sid = sensor["id"]
    with db.pool.connection() as conn:
        s = conn.execute(_SUMMARY, (sid,)).fetchone()
        bands = conn.execute(_BY_BAND, (sid,)).fetchall()
    return {
        "aps": s["aps"],
        "readings": s["readings"],
        "computed_at": s["computed_at"],
        "criteria": {"min_obs": BASELINE_MIN_OBS, "min_days": BASELINE_MIN_DAYS},
        "sd": {"median": s["sd_median"], "q3": s["sd_q3"], "p90": s["sd_p90"]},
        "robust_sd": {"median": s["rsd_median"], "q3": s["rsd_q3"], "p90": s["rsd_p90"]},
        "within": [{"db": t, "sd": s["sd_le_%d" % t], "robust_sd": s["rsd_le_%d" % t]}
                   for t in THRESHOLDS_DB],
        "by_band": bands,
    }
