"""GET /api/v2/aps, /api/v2/aps/{bssid}, /aps/{bssid}/rssi, /aps/{bssid}/load
(Aps and ApDetail boards).

The list holds the account APs seen by the selected sensors in the period:
counts for the chips, filters (q on SSID / BSSID / vendor, classification,
band, open, new in the period, hide hotspots = mobile APs), sort, grouping
by network or by the strongest sensor, offset pages (sorted, small, not a
feed). The AP page: identity, security and policy, load as last measured,
classification, per-sensor baseline and observations, configuration
history, events; the RSSI chart (hourly medians per sensor, D8) and the
load charts (utilization per bucket, clients as an average week).
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from .. import apcache, deps, periods, privacy, queries, shape
from ..periods import iso

router = APIRouter(prefix="/aps", tags=["v2 access points"])

_Q = queries.load("aps")
_QC = queries.load("context")

BSSID_RE = r"^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$"
CLASSES = ("managed", "friendly", "unknown", "suspicious", "ignored")
ACTIVE = timedelta(minutes=5)
SORTS = {
    "last_seen": lambda a: a["last_seen"],
    "first_seen": lambda a: a["first_seen"],
    "ssid": lambda a: ((a["network_name"] or "￿").lower(), a["bssid"]),
    "bssid": lambda a: a["bssid"],
    "vendor": lambda a: ((a["manuf"] or "￿").lower(), a["bssid"]),
    "classification": lambda a: (CLASSES.index(a["classification"]), a["bssid"]),
    "security": lambda a: (shape.SECURITY_ORDER.get(a["security"], 9), a["bssid"]),
    "channel": lambda a: (_channel_num(a["channel"]), a["bssid"]),
    "clients": lambda a: (a["clients"] if a["clients"] is not None else -1, a["bssid"]),
    "util": lambda a: (a["util"] if a["util"] is not None else -1.0, a["bssid"]),
    "rssi": lambda a: (_strongest(a)[1], a["bssid"]),
}


def _channel_num(ch):
    try:
        return int(ch)
    except (TypeError, ValueError):
        return 10_000


def _strongest(a):
    """(sensor, median) of the strongest sensor that saw the AP."""
    for s in a["seen_by"]:
        if s["rssi_median"] is not None:
            return s["sensor"], float(s["rssi_median"])
    return (a["seen_by"][0]["sensor"] if a["seen_by"] else None), -999.0


def _is_new(a, period_start):
    """First seen in the period, and not in a sensor's warm-up (decided 2026-10-12)."""
    return a["first_seen"] >= period_start and not a["warmup"]


def _ap_item(a, active_after, period_start):
    policy_met = None if not a["has_policy"] else a["policy_reason"] is None
    return {
        "bssid": a["bssid"], "ssid": a["ssid"], "hidden": a["hidden"], "network": a["network_name"],
        "manuf": a["manuf"], "random_bssid": a["random_bssid"], "classification": a["classification"],
        "classified_by": a["classified_by"], "security": a["security"], "label": a["security_label"],
        "policy_met": policy_met, "policy_reason": a["policy_reason"], "channel": a["channel"], "band": a["band"],
        "ht_mode": a["ht_mode"], "clients": a["clients"],
        "util": None if a["util"] is None else round(a["util"], 1),
        "seen_by": [{"sensor": s["sensor"],
                     "rssi_median": None if s["rssi_median"] is None else float(s["rssi_median"])}
                    for s in a["seen_by"]],
        "first_seen": iso(a["first_seen"]), "last_seen": iso(a["last_seen"]),
        "active": active_after is not None and a["last_seen"] >= active_after,
        "mobile": a["mobile"], "new": _is_new(a, period_start),
    }


@router.get("")
def list_aps(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
             range_: str = Depends(deps.range_query),
             q: str | None = Query(None, max_length=64, description="SSID, BSSID or vendor substring"),
             cls: str | None = Query(None, alias="class", max_length=64,
                                     description="csv: managed,friendly,unknown,suspicious,ignored"),
             band: str | None = Query(None, pattern=r"^(2\.4|5)$"),
             open_: bool = Query(False, alias="open"), new: bool = Query(False),
             hide_hotspots: bool = Query(True), group: str = Query("none", pattern="^(none|ssid|sensor)$"),
             sort: str = Query("-last_seen", max_length=20), page: int = Query(1, ge=1, le=10_000),
             page_size: int = Query(50, ge=1, le=100)):
    classes = [c.strip() for c in (cls or "").split(",") if c.strip()]
    if any(c not in CLASSES for c in classes):
        raise HTTPException(status_code=422, detail="class: one of %s" % ", ".join(CLASSES))
    key = sort.lstrip("-")
    if key not in SORTS:
        raise HTTPException(status_code=422, detail="sort: one of %s (- for descending)" % ", ".join(SORTS))
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch, envelope=p.envelope(sel["filter"]))
    if cached:
        return cached
    with deps.connection() as conn:
        aps = apcache.account_aps(conn, acc, _Q["account_aps"])
        seen = deps.run(conn, _Q, "seen_by", sensor_ids=sel["ids"], **{"from": p.start, "to": p.ends[-1]})
        header = deps.run(conn, _Q, "header", account_id=acc["id"], sensor_ids=sel["ids"], latest=sel["latest"])[0]
    base = []
    for s in seen:
        a = aps.get(s["ap_id"])
        if a is not None:
            base.append(dict(a, seen_by=s["seen_by"]))
    if q:
        needle = q.lower().replace("-", ":")
        base = [a for a in base if needle in (a["network_name"] or "").lower() or needle in a["bssid"].lower()
                or needle in (a["manuf"] or "").lower()]
    counts = {"all": len(base)}
    counts.update({c: sum(1 for a in base if a["classification"] == c) for c in CLASSES})
    counts.update({"band_2_4": sum(1 for a in base if a["band"] == "2.4"),
                   "band_5": sum(1 for a in base if a["band"] == "5"),
                   "open": sum(1 for a in base if a["security"] == "open"),
                   "new": sum(1 for a in base if _is_new(a, p.start)),
                   "hotspots": sum(1 for a in base if a["mobile"])})
    rows = base
    if classes:
        rows = [a for a in rows if a["classification"] in classes]
    if band:
        rows = [a for a in rows if a["band"] == band]
    if open_:
        rows = [a for a in rows if a["security"] == "open"]
    if new:
        rows = [a for a in rows if _is_new(a, p.start)]
    hidden_hotspots = 0
    if hide_hotspots:
        hidden_hotspots = sum(1 for a in rows if a["mobile"])
        rows = [a for a in rows if not a["mobile"]]
    rows.sort(key=SORTS[key], reverse=sort.startswith("-"))
    groups = None
    if group != "none":
        gkey = (lambda a: a["network_key"]) if group == "ssid" else (lambda a: _strongest(a)[0] or "")
        sizes = {}
        for a in rows:
            sizes[gkey(a)] = sizes.get(gkey(a), 0) + 1
        rows.sort(key=lambda a: (-sizes[gkey(a)], gkey(a)))           # stable: the sort stays within a group
        groups = [{"key": k, "n": n} for k, n in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))]
    active_after = sel["latest"] - ACTIVE if sel["latest"] else None
    first = (page - 1) * page_size
    items = []
    for a in rows[first:first + page_size]:
        item = _ap_item(a, active_after, p.start)
        if groups is not None:
            item["group"] = a["network_key"] if group == "ssid" else _strongest(a)[0]
        items.append(item)
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc))
    body.update({
        "header": header, "counts": counts, "total": len(rows), "hidden_hotspots": hidden_hotspots,
        "page": page, "page_size": page_size, "sort": sort, "group": group, "groups": groups, "items": items,
    })
    return deps.respond(body, tag)


def _find(conn, acc, bssid):
    found = deps.run(conn, _Q, "ap", account_id=acc["id"], bssid=bssid)
    if not found:
        raise HTTPException(status_code=404, detail="unknown access point")
    return found[0]


@router.get("/{bssid}")
def get_ap(request: Request, bssid: str = Path(pattern=BSSID_RE), acc=Depends(deps.get_account)):
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        a = _find(conn, acc, bssid)
        sensors = deps.run(conn, _Q, "ap_sensors", ap_id=a["ap_id"])
        history = deps.run(conn, _Q, "config_history", ap_id=a["ap_id"])
        events = deps.run(conn, _Q, "events", account_id=acc["id"], bssid=bssid)
        allowed = deps.allowed_bssids(conn, acc, [a["bssid"]] + [e["summary"] for e in events])
    days = sum(s["days_seen"] for s in sensors)
    channel_changes = sum(s["channel_changes"] for s in sensors)
    policy = None
    if a["network_kind"] == "managed":
        policy = {"network": a["network_name"], "expected_security": a["expected_security"],
                  "require_mfp": a["require_mfp"], "met": a["policy_reason"] is None, "reason": a["policy_reason"]}
    body = {
        "identity": {"bssid": a["bssid"], "oui": a["oui"], "ssid": a["ssid"], "hidden": a["hidden"],
                     "name_seen": a["name_seen"], "network": a["network_name"], "network_kind": a["network_kind"],
                     "manuf": a["manuf"], "random_bssid": a["random_bssid"], "mobile": a["mobile"],
                     "channel": a["channel"], "band": a["band"], "ht_mode": a["ht_mode"], "country": a["country"],
                     "first_seen": iso(a["first_seen"]), "last_seen": iso(a["last_seen"])},
        "security": {"level": a["security"], "label": a["security_label"], "crypt": a["crypt"],
                     "detail": a["detail"], "mfp": a["mfp"], "policy": policy},
        # as last measured (devices.cur_*); clients: the BSS Load station count when the AP sends one
        "load": {"clients": a["clients"], "kismet_clients": a["cur_n_clients"],
                 "qbss_stations": a["cur_qbss_stations"],
                 "util": None if a["cur_util_pct"] is None else round(a["cur_util_pct"], 1), "at": iso(a["cur_at"]),
                 "sensor": a["load_sensor"]},
        "classification": {"value": a["classification"], "by": a["classified_by"],
                           "manual": a["manual_classification"], "at": iso(a["classified_at"])},
        "sensors": [{
            "name": s["sensor"],
            "baseline": None if not s["has_baseline"] or s["rssi_median"] is None else {
                "median": s["rssi_median"], "robust_sd": s["rssi_robust_sd"],
                "floor_share": (round(s["baseline_n_floor"] / (s["baseline_n_obs"] + s["baseline_n_floor"]), 4)
                                if (s["baseline_n_obs"] or 0) + (s["baseline_n_floor"] or 0) else None),
                "n_obs": s["baseline_n_obs"], "n_days": s["baseline_n_days"],
                "window": {"from": iso(s["baseline_window_start"]), "to": iso(s["baseline_window_end"])},
                "computed_at": iso(s["baseline_at"]), "trusted": s["trusted"]},
            "rssi_typical": s["rssi_typical"], "obs_7d": s["obs_7d"],
            "first_seen": iso(s["first_seen"]), "last_seen": iso(s["last_seen"]),
            "first_capture": iso(s["first_obs"]), "last_capture": iso(s["last_obs"]),
        } for s in sensors],
        "config_history": {"total": sum(s["n_changes"] for s in sensors),
                           "rows": [{"ts": iso(h["ts"]), "sensor": h["sensor"], "changes": h["changes"]}
                                    for h in history]},
        "channel_changes_per_day": round(channel_changes / days, 1) if days else None,
        "first_capture": [{"sensor": s["sensor"], "ts": iso(s["first_obs"])} for s in sensors],
        "events": [{"id": e["id"], "ts": iso(e["ts"]), "ts_end": iso(e["ts_end"]), "severity": e["severity"],
                    "kind": e["kind"], "type": e["type"], "summary": e["summary"], "sensors": [e["sensor"]],
                    "status": e["status"], "status_at": iso(e["status_at"]), "status_note": e["status_note"]}
                   for e in events],
        "note": {"text": a["note"], "at": iso(a["note_at"])},
    }
    return deps.respond(privacy.redact(body, allowed), tag)


@router.get("/{bssid}/rssi")
def ap_rssi(request: Request, bssid: str = Path(pattern=BSSID_RE), days: int = Query(7, ge=1, le=30),
            acc=Depends(deps.get_account)):
    """Hourly RSSI medians per sensor (D8) over the `days` before the AP was
    last seen, on a full hourly grid (null = no valid reading that hour)."""
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        a = _find(conn, acc, bssid)
        end = a["last_seen"].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        start = end - timedelta(days=days)
        rows = deps.run(conn, _Q, "rssi", ap_id=a["ap_id"], **{"from": start, "to": end})
        sensors = deps.run(conn, _Q, "ap_sensors", ap_id=a["ap_id"])
    n = int((end - start).total_seconds() // 3600)
    names = [s["sensor"] for s in sensors]
    series = {name: [None] * n for name in names}
    floors = {name: [0] * n for name in names}
    for r in rows:
        i = int((r["hour"] - start).total_seconds() // 3600)
        series[r["sensor"]][i] = r["median"]
        floors[r["sensor"]][i] = r["n_floor"]
    body = {
        "bssid": a["bssid"], "from": iso(start), "to": iso(end), "bucket_s": 3600,
        "t": [int((start + timedelta(hours=i)).timestamp()) for i in range(n)],
        "series": series,
        # floor readings (-106/-120 dBm) are censored, not levels: counted apart
        "n_floor": floors,
        "baseline": {s["sensor"]: {"median": s["rssi_median"], "robust_sd": s["rssi_robust_sd"]}
                     for s in sensors if s["has_baseline"] and s["rssi_median"] is not None},
    }
    return deps.respond(body, tag)


@router.get("/{bssid}/load")
def ap_load(request: Request, bssid: str = Path(pattern=BSSID_RE),
            view: str = Query("util", pattern="^(util|clients_week)$"),
            range_: str = Depends(deps.range_query), weeks: int = Query(4, ge=1, le=8),
            acc=Depends(deps.get_account)):
    """util: utilization of the AP's channel (its BSS Load reports) per
    bucket of the period, average and hourly peak; clients_week: its clients
    per local weekday and hour, the mean over the last `weeks` weeks."""
    now = shape.utc_now()
    p = (deps.period_of(acc, range_) if view == "util" else periods.days_window(acc["tz"], now, 7 * weeks))
    tag, cached = deps.etag(request, acc, p.epoch, envelope=p.envelope("all"))
    if cached:
        return cached
    with deps.connection() as conn:
        a = _find(conn, acc, bssid)
        ids = deps.run(conn, _Q, "sensor_ids", ap_id=a["ap_id"])[0]["ids"] or []
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=ids, **{"from": p.start, "to": p.ends[-1]})
        load = deps.run(conn, _Q, "load", ap_id=a["ap_id"], **{"from": p.start, "to": p.ends[-1]})
    sensors = [s for s in acc["sensors"] if s["id"] in ids]
    body = dict(p.envelope("all"), bssid=a["bssid"], view=view)
    if view == "util":
        n = len(p.starts)
        sums, polls, peak = [0.0] * n, [0] * n, [None] * n
        for r in load:
            i = shape.bucket_of(p, r["hour"])
            if i is None or not r["util_polls"]:
                continue
            sums[i - 1] += r["util_sum"]
            polls[i - 1] += r["util_polls"]
            hourly = r["util_sum"] / r["util_polls"]
            peak[i - 1] = hourly if peak[i - 1] is None else max(peak[i - 1], hourly)
        buckets, on = shape.buckets(p, shape.coverage(p, hours, sensors))
        body.update({"buckets": buckets,
                     "avg": shape.series([round(sums[i] / polls[i], 1) if polls[i] else None for i in range(n)], on),
                     "peak": shape.series([None if v is None else round(v, 1) for v in peak], on),
                     "reports_util": any(r["util_polls"] for r in load)})
    else:
        clients = {r["hour"]: {"total": r["clients"], "by_network": {}} for r in load if r["clients"] is not None}
        rows, peak = shape.heatmap(p, acc["tz"], clients, set(clients), shape.captured_hours(hours), "week")
        body.update({"weeks": weeks, "rows": rows, "peak": peak, "ramp": list(shape.RAMP)})
    return deps.respond(body, tag)
