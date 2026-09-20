"""GET /api/aps, /api/aps/{device_key}, /api/aps/{device_key}/rssi.

The inventory comes from the ap_inventory view (schema.sql): devices of type
'ap' with the derived OUI / randomised-BSSID / hidden flags and the baseline
joined in. The RSSI series is bucketed in SQL - raw per-poll rows never leave
the database.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from .. import db
from ..deps import DEVICE_KEY_RE, epoch, get_sensor

router = APIRouter(prefix="/api", tags=["access points"])

_AP_COLS = """
    device_key, upper(bssid::text) AS bssid, upper(left(bssid::text, 8)) AS oui,
    random_bssid, hidden, ssid, manuf, crypt, adv_channel, ht_mode,
    first_seen, last_seen, extract(epoch FROM lifetime)::float8 AS lifetime_s,
    coalesce(trusted, false) AS trusted, rssi_median, rssi_robust_sd, rssi_sd,
    baseline_n_obs, baseline_n_days"""

_CONFIG_COLS = "ssid, crypt, adv_channel, ht_mode, beacon_rate, country, cloaked, mfp_sup, mfp_req"


@router.get("/aps")
def list_aps(sensor=Depends(get_sensor)):
    """Every AP the sensor has seen (all rows; sort/filter client-side)."""
    with db.pool.connection() as conn:
        rows = conn.execute(
            "SELECT " + _AP_COLS + " FROM ap_inventory WHERE sensor_id = %s "
            "ORDER BY last_seen DESC", (sensor["id"],)).fetchall()
    return {"count": len(rows), "aps": rows}


@router.get("/aps/{device_key}")
def get_ap(device_key: str = Path(pattern=DEVICE_KEY_RE), sensor=Depends(get_sensor)):
    """One AP: identity + current config, baseline, observation summary, config history."""
    sid = sensor["id"]
    with db.pool.connection() as conn:
        ap = conn.execute(
            "SELECT " + _AP_COLS + ", cloaked, mfp_sup, mfp_req, beacon_rate, country, "
            "config_changed_at FROM ap_inventory WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        if ap is None:
            raise HTTPException(status_code=404, detail="unknown access point")
        baseline = conn.execute(
            "SELECT rssi_median, rssi_mean, rssi_sd, rssi_robust_sd, rssi_p5, rssi_p95, "
            "main_freq_khz, n_obs, n_days, window_start, window_end, computed_at, trusted, "
            "trusted_at, note FROM ap_baselines WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        obs = conn.execute(
            "SELECT count(*) AS n_obs, count(rssi) AS n_rssi, min(ts) AS first_obs, "
            "max(ts) AS last_obs FROM observations WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        hist_total = conn.execute(
            "SELECT count(*) AS n FROM device_config_history "
            "WHERE sensor_id = %s AND device_key = %s", (sid, device_key)).fetchone()["n"]
        hist = conn.execute(
            "SELECT ts, " + _CONFIG_COLS + " FROM device_config_history "
            "WHERE sensor_id = %s AND device_key = %s ORDER BY ts DESC LIMIT 200",
            (sid, device_key)).fetchall()
    return {
        "ap": ap,
        "baseline": baseline,
        "observations": obs,
        # rows hold the configuration that was REPLACED at ts (as on the sensor)
        "config_history": {"total": hist_total, "rows": hist},
    }


@router.get("/aps/{device_key}/rssi")
def ap_rssi(device_key: str = Path(pattern=DEVICE_KEY_RE),
            bucket: int = Query(3600, ge=300, le=86400, description="bucket width, seconds"),
            since: datetime | None = Query(None, alias="from"),
            until: datetime | None = Query(None, alias="to"),
            sensor=Depends(get_sensor)):
    """RSSI over time, aggregated per bucket: median / min / max / count.

    Columnar arrays (t = unix seconds of the bucket start) plus the AP's
    baseline so the chart can draw the reference line without a second call.
    """
    sid = sensor["id"]
    where = ["sensor_id = %s", "device_key = %s", "rssi IS NOT NULL"]
    params = [bucket, sid, device_key]
    if since is not None:
        where.append("ts >= %s")
        params.append(since)
    if until is not None:
        where.append("ts < %s")
        params.append(until)
    sql = ("SELECT date_bin(make_interval(secs => %s), ts, '2000-01-01') AS t, "
           "count(rssi) AS n, "
           "percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi) AS median, "
           "min(rssi) AS min, max(rssi) AS max "
           "FROM observations WHERE " + " AND ".join(where) + " GROUP BY 1 ORDER BY 1")
    with db.pool.connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM devices WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="unknown device")
        baseline = conn.execute(
            "SELECT rssi_median AS median, rssi_robust_sd AS robust_sd, rssi_sd AS sd "
            "FROM ap_baselines WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        rows = conn.execute(sql, params).fetchall()
    return {
        "bucket_s": bucket,
        "baseline": baseline,
        "t": [epoch(r["t"]) for r in rows],
        "median": [r["median"] for r in rows],
        "min": [r["min"] for r in rows],
        "max": [r["max"] for r in rows],
        "n": [r["n"] for r in rows],
    }
