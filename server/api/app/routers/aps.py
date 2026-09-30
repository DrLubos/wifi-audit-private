"""GET /api/aps, /api/aps/{device_key}, /api/aps/{device_key}/rssi.

Schema 6: the inventory and the AP page header come from ap_summary, the
hourly RSSI series from ap_rssi_hourly and the configuration changes from the
materialized ap_config_changes - all built by refresh_rollups() at import
time. Raw observations are read only for 15-minute buckets over at most 48 h
(an index-only range scan). Every response carries the ETag of the sensor's
last refresh.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response

from .. import cache, db
from ..deps import (DEVICE_KEY_RE, epoch, get_sensor, not_modified, require_rollups,
                    rollup_etag, set_cache_headers)

router = APIRouter(prefix="/api", tags=["access points"])

RAW_BUCKET_S = 900
HOURLY_BUCKET_S = 3600
RAW_MAX_WINDOW = timedelta(hours=48)

_LIST_COLS = """
    device_key, upper(bssid::text) AS bssid, upper(left(bssid::text, 8)) AS oui,
    random_bssid, hidden, ssid, manuf, crypt, adv_channel, ht_mode,
    first_seen, last_seen, extract(epoch FROM last_seen - first_seen)::float8 AS lifetime_s,
    trusted, rssi_median, rssi_robust_sd, rssi_sd, baseline_n_obs, baseline_n_days"""

# The whole list as one JSON text built by PostgreSQL: ~600 rows x 19 fields
# through FastAPI's encoder cost ~50 ms of the box's CPU per request (warm
# HTTP p50 54 ms against 4 ms of SQL, measured 2026-09-29).
_LIST = """
    SELECT json_build_object(
             'count', count(*),
             'aps', coalesce(json_agg(a ORDER BY a.last_seen DESC, a.device_key), '[]'::json))::text AS body
    FROM (SELECT """ + _LIST_COLS + """ FROM ap_summary WHERE sensor_id = %s) a"""

_AP = """
    SELECT """ + _LIST_COLS + """, cloaked, mfp_sup, mfp_req, beacon_rate, country,
           config_changed_at, hidden_beacon, name_seen, has_baseline,
           rssi_mean, rssi_p5, rssi_p95, main_freq_khz, baseline_n_floor,
           baseline_window_start, baseline_window_end, baseline_at, trusted_at, note,
           n_obs, n_valid, n_floor, first_obs, last_obs, history_rows, n_changes,
           (SELECT country_expected FROM sensor_summary ss WHERE ss.sensor_id = a.sensor_id)
             AS country_expected
    FROM ap_summary a WHERE sensor_id = %s AND device_key = %s"""

# Configuration changes of one AP, newest first, one row per poll ts with the
# fields that changed there (old -> new): the cleaned reading of
# device_config_history (schema.sql, ap_config_changes).
_CHANGES = """
    SELECT ts, json_agg(json_build_object('field', field, 'old', old_value, 'new', new_value)
                        ORDER BY field) AS changes
    FROM ap_config_changes WHERE sensor_id = %s AND device_key = %s
    GROUP BY ts ORDER BY ts DESC LIMIT 200"""

_AP_KEYS = ("device_key", "bssid", "oui", "random_bssid", "hidden", "ssid", "manuf", "crypt",
            "adv_channel", "ht_mode", "first_seen", "last_seen", "lifetime_s", "trusted",
            "rssi_median", "rssi_robust_sd", "rssi_sd", "baseline_n_obs", "baseline_n_days",
            "cloaked", "mfp_sup", "mfp_req", "beacon_rate", "country", "config_changed_at",
            "country_expected", "hidden_beacon", "name_seen")

# Hours with at least one reading (valid or floor), as the per-poll query had them.
_HOURLY = """
    SELECT hour AS t, median, p10, p90, min, max, n_valid AS n, n_floor
    FROM ap_rssi_hourly
    WHERE sensor_id = %s AND device_key = %s AND n_valid + n_floor > 0{window}
    ORDER BY hour"""

# rssi_valid(): the adapter's floor values are censored readings, never a level
# (schema.sql); counted per bucket as n_floor. Index-only range scan on
# observations_device_ts_rssi_floor.
_RAW = """
    SELECT date_bin(interval '900 seconds', ts, timestamptz '2000-01-01 00:00:00+00') AS t,
           count(rssi_valid(rssi)) AS n,
           count(*) FILTER (WHERE rssi_is_floor(rssi, rssi_floor)) AS n_floor,
           percentile_cont(ARRAY[0.5, 0.1, 0.9]) WITHIN GROUP (ORDER BY rssi_valid(rssi)) AS pct,
           min(rssi_valid(rssi)) AS min, max(rssi_valid(rssi)) AS max
    FROM observations
    WHERE sensor_id = %s AND device_key = %s AND (rssi IS NOT NULL OR rssi_floor)
      AND ts >= %s AND ts < %s
    GROUP BY 1 ORDER BY 1"""


@router.get("/aps")
def list_aps(request: Request, sensor=Depends(get_sensor)):
    """Every AP the sensor has seen (all rows; sort/filter client-side)."""
    require_rollups(sensor)
    etag = rollup_etag(sensor)
    cached = not_modified(request, etag)
    if cached:
        return cached
    with db.pool.connection() as conn:
        body = conn.execute(_LIST, (sensor["id"],)).fetchone()["body"]
    return Response(content=body, media_type="application/json", headers=cache.headers(etag))


@router.get("/aps/{device_key}")
def get_ap(request: Request, response: Response,
           device_key: str = Path(pattern=DEVICE_KEY_RE), sensor=Depends(get_sensor)):
    """One AP: identity + current config, baseline, observation summary, config history."""
    require_rollups(sensor)
    etag = rollup_etag(sensor)
    cached = not_modified(request, etag)
    if cached:
        return cached
    sid = sensor["id"]
    with db.pool.connection() as conn:
        s = conn.execute(_AP, (sid, device_key)).fetchone()
        if s is None:
            raise HTTPException(status_code=404, detail="unknown access point")
        changes = conn.execute(_CHANGES, (sid, device_key)).fetchall() if s["n_changes"] else []
    set_cache_headers(response, etag)

    ap = {k: s[k] for k in _AP_KEYS}
    # the sensor's usual country (most common among its APs): another one is a
    # weak rogue / misconfigured-AP hint, shown as a marker
    ap["country_foreign"] = (s["country"] is not None and s["country_expected"] is not None
                             and s["country"] != s["country_expected"])
    baseline = None
    if s["has_baseline"]:
        baseline = {
            "rssi_median": s["rssi_median"], "rssi_mean": s["rssi_mean"], "rssi_sd": s["rssi_sd"],
            "rssi_robust_sd": s["rssi_robust_sd"], "rssi_p5": s["rssi_p5"], "rssi_p95": s["rssi_p95"],
            "main_freq_khz": s["main_freq_khz"], "n_obs": s["baseline_n_obs"],
            "n_floor": s["baseline_n_floor"], "n_days": s["baseline_n_days"],
            "window_start": s["baseline_window_start"], "window_end": s["baseline_window_end"],
            "computed_at": s["baseline_at"], "trusted": s["trusted"],
            "trusted_at": s["trusted_at"], "note": s["note"],
        }
    return {
        "ap": ap,
        "baseline": baseline,
        # floor readings (-106/-120 dBm) are censored, not levels: counted apart
        "observations": {"n_obs": s["n_obs"], "n_rssi": s["n_valid"], "n_floor": s["n_floor"],
                         "first_obs": s["first_obs"], "last_obs": s["last_obs"]},
        # changes[].changes: [{field, old, new}] at ts (old value replaced at ts);
        # raw_rows = rows of device_config_history, incl. the pre-v5 churn
        "config_history": {
            "total": s["n_changes"],
            "raw_rows": s["history_rows"],
            "rows": [{"ts": r["ts"], "changes": r["changes"]} for r in changes],
        },
    }


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


@router.get("/aps/{device_key}/rssi")
def ap_rssi(request: Request, response: Response,
            device_key: str = Path(pattern=DEVICE_KEY_RE),
            bucket: int = Query(HOURLY_BUCKET_S, description=(
                "3600 = hourly, the whole history (from ap_rssi_hourly); 900 = 15-minute "
                "buckets from the raw observations, needs from and to at most 48 h apart")),
            since: datetime | None = Query(None, alias="from", description="UTC when no offset"),
            until: datetime | None = Query(None, alias="to", description="exclusive"),
            sensor=Depends(get_sensor)):
    """RSSI over time per bucket: median, p10, p90, min, max, count, floor count.

    Columnar arrays (t = unix seconds of the bucket start) plus the AP's
    baseline so the chart can draw the reference line without a second call.
    """
    since, until = _utc(since), _utc(until)
    if bucket not in (HOURLY_BUCKET_S, RAW_BUCKET_S):
        raise HTTPException(status_code=422, detail="bucket must be 3600 (hourly) or 900 (15 min)")
    if bucket == RAW_BUCKET_S:
        if since is None or until is None:
            raise HTTPException(status_code=422, detail="bucket=900 needs from and to")
        if not timedelta(0) < until - since <= RAW_MAX_WINDOW:
            raise HTTPException(status_code=422,
                                detail="bucket=900: to must be after from, at most 48 h")
    require_rollups(sensor)
    etag = rollup_etag(sensor)
    cached = not_modified(request, etag)
    if cached:
        return cached
    sid = sensor["id"]
    with db.pool.connection() as conn:
        ap = conn.execute(
            "SELECT has_baseline, rssi_median, rssi_robust_sd, rssi_sd FROM ap_summary "
            "WHERE sensor_id = %s AND device_key = %s", (sid, device_key)).fetchone()
        if ap is None:
            raise HTTPException(status_code=404, detail="unknown access point")
        if bucket == HOURLY_BUCKET_S:
            window, params = "", [sid, device_key]
            if since is not None:
                window += " AND hour >= %s"
                params.append(since)
            if until is not None:
                window += " AND hour < %s"
                params.append(until)
            rows = conn.execute(_HOURLY.format(window=window), params).fetchall()
        else:
            rows = conn.execute(_RAW, (sid, device_key, since, until)).fetchall()
            for r in rows:   # pct is NULL for a bucket of floor readings only
                r["median"], r["p10"], r["p90"] = r.pop("pct") or (None, None, None)
    set_cache_headers(response, etag)
    baseline = None
    if ap["has_baseline"]:
        baseline = {"median": ap["rssi_median"], "robust_sd": ap["rssi_robust_sd"],
                    "sd": ap["rssi_sd"]}
    return {
        "bucket_s": bucket,
        "baseline": baseline,
        "t": [epoch(r["t"]) for r in rows],
        "median": [r["median"] for r in rows],
        "p10": [r["p10"] for r in rows],
        "p90": [r["p90"] for r in rows],
        "min": [r["min"] for r in rows],
        "max": [r["max"] for r in rows],
        "n": [r["n"] for r in rows],
        "n_floor": [r["n_floor"] for r in rows],
    }
