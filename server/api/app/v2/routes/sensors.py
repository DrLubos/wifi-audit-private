"""GET /api/v2/sensors - status, capture, uploads and configuration per sensor.

Feeds the sidebar, the top bar ("Latest data") and the sensor cards of
Overview and Sensors. Status is online / offline against the account's
sensor_offline_min from the last collecting poll, no_data before the first
poll; until live ingest every sensor reads offline between imports, so the
cards show data_until (SPEC Q7). The 7-day and 24-hour windows end at the
sensor's last poll (sql/sensors.sql). ETag epoch: the current minute.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from .. import deps, periods, queries, shape
from ..periods import iso

router = APIRouter(tags=["v2 sensors"])

_Q = queries.load("sensors")
_QC = queries.load("context")


def _ts(v):
    return iso(shape.parse_ts(v))


def _config(c):
    if c is None:
        return None
    c = dict(c)
    for k in ("confirmed_at", "created_at"):
        if k in c:
            c[k] = _ts(c[k])
    return c


@router.get("/sensors")
def sensors(request: Request, acc=Depends(deps.get_account)):
    tag, cached = deps.etag(request, acc, deps.minute_epoch())
    if cached:
        return cached
    now = shape.utc_now()
    polled = [s for s in acc["sensors"] if s["last_poll"]]
    with deps.connection() as conn:
        rows = deps.run(conn, _Q, "sensors", account_id=acc["id"])
        hours = []
        if polled:
            hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=[s["id"] for s in polled],
                             **{"from": min(s["last_poll"] for s in polled) - timedelta(hours=170),
                                "to": max(s["last_poll"] for s in polled) + timedelta(hours=1)})
    by_id = {s["id"]: s for s in acc["sensors"]}
    out = []
    for r in rows:
        status, offline_s = shape.sensor_status(r["last_capture_at"], r["last_poll"], now, acc["offline_min"])
        coverage, window = None, None
        if r["last_poll"] is not None:
            start = r["last_poll"].replace(minute=0, second=0, microsecond=0) - timedelta(hours=167)
            end = r["last_poll"] + timedelta(seconds=shape.POLL_S)
            coverage = shape.window_share(hours, by_id[r["id"]], start, end)
            window = {"from": iso(start), "to": iso(r["last_poll"])}
        out.append({
            "name": r["name"], "location": r["location"], "hardware": r["hardware"],
            "status": status, "offline_s": offline_s,
            "data_until": iso(r["last_poll"]), "first_poll": iso(r["first_poll"]),
            "last_poll": iso(r["last_poll"]), "last_capture_at": iso(r["last_capture_at"]),
            # the hop list of the last collecting poll (sensor_summary)
            "hop": (None if r["last_hop_n"] is None else
                    {"n": r["last_hop_n"], "visited": r["last_hop_visited"], "ok": r["last_hop_ok"]}),
            "poll_s": None if r["poll_s"] is None else round(float(r["poll_s"]), 1),
            "last_upload": (None if r["last_upload"] is None else
                            dict(r["last_upload"], at=_ts(r["last_upload"]["at"]),
                                 polls_to=_ts(r["last_upload"]["polls_to"]))),
            "coverage_7d": coverage, "window_7d": window,
            "crashes_24h": {"count": r["crashes"] or 0, "max_recovery_s": r["max_recovery_s"],
                            "unrecovered_since": iso(r["unrecovered_since"])},
            "aps_7d": r["aps_7d"] or 0, "aps_only_here_7d": r["aps_only_here_7d"] or 0,
            "config": _config(r["config"]), "pending": _config(r["pending"]),
        })
    last_polls = [s["last_poll"] for s in acc["sensors"] if s["last_poll"]]
    body = {
        "account": deps.account_head(acc),
        "latest_data": iso(max(last_polls)) if last_polls else None,
        "offline_after_min": acc["offline_min"],
        # write endpoints come with phase 5 (WRITE_TOKEN); until then locked
        "write": {"enabled": False},
        "sensors": out,
    }
    return deps.respond(body, tag)


def _sensor(conn, acc, name):
    found = deps.run(conn, _Q, "sensor", account_id=acc["id"], name=name)
    if not found:
        raise HTTPException(status_code=404, detail="unknown sensor")
    return found[0]


@router.get("/sensors/{name}/coverage")
def coverage(request: Request, name: str = Path(max_length=64), days: int = Query(14, ge=1, le=60),
             acc=Depends(deps.get_account)):
    """Shares of each of the last `days` local days (today partial): ok,
    limited, outage, not installed, no data (after the last import)."""
    tag, cached = deps.etag(request, acc, deps.minute_epoch())
    if cached:
        return cached
    now = shape.utc_now()
    window = periods.local_days(acc["tz"], now, days)
    with deps.connection() as conn:
        s = _sensor(conn, acc, name)
        span = {"from": window[0][1], "to": window[-1][2]}
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=[s["id"]], **span)
        notes = deps.run(conn, _Q, "coverage_notes", sensor_id=s["id"], **span)
    limited = [(n["starts_at"], n["ends_at"]) for n in notes]
    body = {"sensor": s["name"], "tz": acc["tz"], "first_poll": iso(s["first_poll"]),
            "data_until": iso(s["last_poll"]), "days": shape.day_coverage(window, hours, s, now, limited)}
    return deps.respond(body, tag)


@router.get("/sensors/{name}/outages")
def outages(request: Request, name: str = Path(max_length=64), acc=Depends(deps.get_account)):
    """Outages and limited periods, newest first: derived from the polls
    (with the documented cause) and documented windows no poll shows."""
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        s = _sensor(conn, acc, name)
        rows = deps.run(conn, _Q, "outages", sensor_id=s["id"])
    body = {"sensor": s["name"], "items": [
        {"type": r["type"], "from": iso(r["starts_at"]), "to": iso(r["ends_at"]), "ongoing": r["ongoing"],
         "duration_s": r["duration_s"], "polls": r["polls"], "source": r["source"], "cause": r["cause"]}
        for r in rows]}
    return deps.respond(body, tag)
