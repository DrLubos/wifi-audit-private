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

# Configuration changes of one AP, newest first, one row per poll ts with the
# fields that changed there (old -> new). ap_config_changes (schema.sql) is the
# cleaned reading of device_config_history: pre-v5 incomplete-record churn,
# Kismet's missing-field crypt_bits 0 and the hidden-beacon alternation are
# unknown values, not changes. total = change events before the LIMIT.
_CHANGES = """
    SELECT ts, changes, count(*) OVER () AS total
    FROM (SELECT ts, json_agg(json_build_object('field', field, 'old', old_value, 'new', new_value)
                              ORDER BY field) AS changes
          FROM ap_config_changes WHERE sensor_id = %s AND device_key = %s
          GROUP BY ts) g
    ORDER BY ts DESC LIMIT 200"""


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
            "config_changed_at, "
            # the sensor's usual country (most common among its APs): another one is
            # a weak rogue / misconfigured-AP hint, shown as a marker
            "(SELECT mode() WITHIN GROUP (ORDER BY c.country) FROM devices c "
            " WHERE c.sensor_id = i.sensor_id AND c.type = 'ap' AND c.country IS NOT NULL) "
            "AS country_expected "
            "FROM ap_inventory i WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        if ap is None:
            raise HTTPException(status_code=404, detail="unknown access point")
        ap["country_foreign"] = (ap["country"] is not None and ap["country_expected"] is not None
                                 and ap["country"] != ap["country_expected"])
        baseline = conn.execute(
            "SELECT rssi_median, rssi_mean, rssi_sd, rssi_robust_sd, rssi_p5, rssi_p95, "
            "main_freq_khz, n_obs, n_floor, n_days, window_start, window_end, computed_at, trusted, "
            "trusted_at, note FROM ap_baselines WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        obs = conn.execute(
            # floor readings (-106/-120 dBm) are censored, not levels: counted apart
            "SELECT count(*) AS n_obs, count(rssi_valid(rssi)) AS n_rssi, "
            "count(*) FILTER (WHERE rssi_is_floor(rssi, rssi_floor)) AS n_floor, "
            "min(ts) AS first_obs, max(ts) AS last_obs "
            "FROM observations WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        raw = conn.execute(
            # hidden APs alternate between the cloaked beacon (ssid '') and a named
            # record (one state in ap_config_changes): flag the hidden beacon and
            # keep the latest name seen for the header
            "SELECT count(*) AS n, coalesce(bool_or(cloaked AND ssid = ''), false) AS hidden_beacon, "
            "(array_agg(ssid ORDER BY ts DESC) FILTER (WHERE ssid <> ''))[1] AS name_seen "
            "FROM device_config_history WHERE sensor_id = %s AND device_key = %s",
            (sid, device_key)).fetchone()
        changes = conn.execute(_CHANGES, (sid, device_key)).fetchall()
    ap["hidden_beacon"] = raw["hidden_beacon"] or bool(ap["cloaked"] and not ap["ssid"])
    ap["name_seen"] = ap["ssid"] or raw["name_seen"]
    return {
        "ap": ap,
        "baseline": baseline,
        "observations": obs,
        # changes[].changes: [{field, old, new}] at ts (old value replaced at ts);
        # raw_rows = rows of device_config_history, incl. the pre-v5 churn
        "config_history": {
            "total": changes[0]["total"] if changes else 0,
            "raw_rows": raw["n"],
            "rows": [{"ts": r["ts"], "changes": r["changes"]} for r in changes],
        },
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
    where = ["sensor_id = %s", "device_key = %s", "(rssi IS NOT NULL OR rssi_floor)"]
    params = [bucket, sid, device_key]
    if since is not None:
        where.append("ts >= %s")
        params.append(since)
    if until is not None:
        where.append("ts < %s")
        params.append(until)
    sql = ("SELECT date_bin(make_interval(secs => %s), ts, '2000-01-01') AS t, "
           # rssi_valid(): the adapter's floor values are censored readings, never
           # a level (schema.sql); they are counted per bucket as n_floor
           "count(rssi_valid(rssi)) AS n, "
           "count(*) FILTER (WHERE rssi_is_floor(rssi, rssi_floor)) AS n_floor, "
           "percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_valid(rssi)) AS median, "
           "min(rssi_valid(rssi)) AS min, max(rssi_valid(rssi)) AS max "
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
        "n_floor": [r["n_floor"] for r in rows],
    }
