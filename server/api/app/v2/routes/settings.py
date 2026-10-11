"""GET /api/v2/settings/{networks|classification|detectors|sensors} - the
stored configuration (Settings and SettingsSensors boards). Writes come with
phase 5 (write token); until then the pages show them locked.
"""

from fastapi import APIRouter, Depends, Request

from .. import deps, queries
from ..periods import iso

router = APIRouter(prefix="/settings", tags=["v2 settings"])

_Q = queries.load("settings")

CLASSIFICATION = ("ignore_random_bssid", "suspicious_unallowed", "managed_by_vendor", "ignore_weaker_dbm",
                  "mobile_vendors")
DETECTORS = ("deauth_min_bursts", "deauth_window_s", "evil_twin_k", "evil_twin_min_db", "evil_twin_guard_s",
             "sensor_offline_min")


def _read(request, acc, build):
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        body = build(conn)
    return deps.respond(body, tag)


@router.get("/networks")
def networks(request: Request, acc=Depends(deps.get_account)):
    def build(conn):
        rows = deps.run(conn, _Q, "networks", account_id=acc["id"])
        return {"networks": [dict(r, created_at=iso(r["created_at"]), updated_at=iso(r["updated_at"]))
                             for r in rows]}
    return _read(request, acc, build)


def _account_settings(conn, acc, keys):
    st = deps.run(conn, _Q, "account_settings", account_id=acc["id"])[0]
    return dict({k: st[k] for k in keys}, updated_at=iso(st["updated_at"]))


@router.get("/classification")
def classification(request: Request, acc=Depends(deps.get_account)):
    return _read(request, acc, lambda conn: _account_settings(conn, acc, CLASSIFICATION))


@router.get("/detectors")
def detectors(request: Request, acc=Depends(deps.get_account)):
    return _read(request, acc, lambda conn: _account_settings(conn, acc, DETECTORS))


@router.get("/sensors")
def sensors(request: Request, acc=Depends(deps.get_account)):
    """Per sensor the applied (confirmed) configuration, a pending one (newer,
    not confirmed - delivery to the sensor does not exist yet, Q10) and every
    version; the documented changes with their data boundaries."""
    def build(conn):
        versions = deps.run(conn, _Q, "sensor_config", account_id=acc["id"])
        history = deps.run(conn, _Q, "sensor_history", account_id=acc["id"])
        out = []
        for s in acc["sensors"]:
            mine = [dict(v, created_at=iso(v["created_at"]), confirmed_at=iso(v["confirmed_at"]))
                    for v in versions if v["sensor"] == s["name"]]
            current = next((v for v in mine if v["confirmed_at"]), None)
            pending = mine[0] if mine and not mine[0]["confirmed_at"] else None
            out.append({"name": s["name"], "current": current, "pending": pending, "versions": mine})
        plans = {s["current"]["channel_plan"] for s in out if s["current"]}
        return {"sensors": out, "mixed_plans": len(plans) > 1,
                "history": [dict(h, ts=iso(h["ts"])) for h in history]}
    return _read(request, acc, build)
