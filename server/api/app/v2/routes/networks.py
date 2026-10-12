"""GET /api/v2/networks, /api/v2/networks/timeseries, /api/v2/clients/heatmap
(Networks board).

networks: the rogue banner (BSSIDs classified suspicious: an unknown BSSID
broadcasting a managed SSID), managed networks (policy, actual security,
compliance - a BSSID of unknown security is not assessed -, sensors, clients
now, BSSID rows), friendly networks, other networks (count, hidden, phone
hotspots summary, presence per network with the median sighting of the
transient ones, paged) and the security changes in the period (without
MFP-only changes of mobile APs). timeseries: networks per bucket as
configured / mobile / other and by their weakest security level. heatmap:
clients on managed networks, day by day or as an average week, on one scale
from the account's 30-day peak.
"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import apcache, deps, periods, queries, shape
from ..periods import iso

router = APIRouter(tags=["v2 networks"])

_Q = queries.load("networks")
_QC = queries.load("context")
_QS = queries.load("settings")
_QA = queries.load("aps")

POLICY = {"enterprise": "Enterprise", "psk_or_better": "WPA2-PSK or better", "wpa3": "WPA3",
          "open_allowed": "Open allowed", None: "No policy"}
OTHER_PAGE = 50
ACTIVE = timedelta(minutes=5)        # an AP is active now: seen within 5 min of its sensor's last capture (Q8)


def _period_args(acc, sel, p):
    return {"account_id": acc["id"], "sensor_ids": sel["ids"], "account_latest": sel["account_latest"],
            "from": p.start, "to": p.ends[-1], "tz": acc["tz"]}


def _policy(n):
    label = POLICY.get(n["expected_security"], n["expected_security"])
    return {"expected_security": n["expected_security"], "require_mfp": n["require_mfp"],
            "label": label + (" + required MFP" if n["require_mfp"] else "")}


def _assessed(b):
    """A BSSID's compliance can be assessed only when its security is known
    (a beacon record); otherwise it is counted as not assessed, never failing
    (decided 2026-10-12)."""
    return b["security"] not in (None, "unknown")


def _bssid_row(b, active_after):
    return {
        "bssid": b["bssid"], "channel": b["channel"], "band": b["band"], "ht_mode": b["ht_mode"],
        "security": b["security"], "label": b["security_label"], "mfp": b["mfp"], "clients": b["clients"],
        "rssi": _rssi(b), "last_seen": iso(b["last_seen"]),
        "active": _active(b, active_after), "classification": b["classification"],
        "classified_by": b["classified_by"], "assessed": _assessed(b),
        "reason": b["reason"] if _assessed(b) else None,
    }


def _rssi(b):
    """{sensor: median of the hourly RSSI medians in the period}; None when
    the sensor had floor readings only (censored, never a level)."""
    return {k: None if v is None else float(v) for k, v in (b["rssi"] or {}).items()}


def _active(b, active_after):
    return any(b["last_seen"] >= t for t in active_after.values()) if active_after else False


@router.get("/networks")
def networks(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
             range_: str = Depends(deps.range_query), other_page: int = Query(1, ge=1, le=1000)):
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch, envelope=p.envelope(sel["filter"]))
    if cached:
        return cached
    a = _period_args(acc, sel, p)
    with deps.connection() as conn:
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=sel["ids"], **{"from": p.start, "to": p.ends[-1]})
        nets = deps.run(conn, _Q, "networks", **a)
        bss = deps.run(conn, _Q, "bssids", **{k: a[k] for k in ("account_id", "sensor_ids", "from", "to")})
        changes = deps.run(conn, _Q, "security_changes",
                           **{k: a[k] for k in ("account_id", "sensor_ids", "account_latest", "from", "to")})
        configured = deps.run(conn, _QS, "networks", account_id=acc["id"])
        # hidden BSSIDs one byte from a visible one: likely the same radio (a hint)
        siblings = shape.radio_siblings(apcache.account_aps(conn, acc, _QA["account_aps"]).values())
        collecting = len(shape.captured_hours(hours))
        names = {n["ssid"] for n in configured}
        others = [n for n in nets if n["network_key"] not in names]
        hotspots = [n for n in others if n["randomized"]]
        listed = [n for n in others if not n["randomized"]]
        first = (other_page - 1) * OTHER_PAGE
        page = [dict(n, presence=shape.presence(n, collecting)) for n in listed[first:first + OTHER_PAGE]]
        # sightings: of the hotspot BSSIDs and of the transient networks on the page
        ids = sorted({i for n in hotspots for i in n["ap_ids"]}
                     | {i for n in page if n["presence"] == "transient" for i in n["ap_ids"]})
        sight = {}
        if ids:
            for r in deps.run(conn, _Q, "sightings", ap_ids=ids, sensor_ids=sel["ids"],
                              **{"from": p.start, "to": p.ends[-1]}):
                sight.setdefault(r["ap_id"], []).append(r["seconds"])
    # active now: seen within 5 min of the last collecting poll of a selected sensor
    active_after = {s["name"]: s["last_capture_at"] - ACTIVE for s in sel["sensors"] if s["last_capture_at"]}
    seen = {n["network_key"]: n for n in nets}

    rogue, by_net = [], {}
    for b in bss:
        if b["classification"] == "suspicious":
            rogue.append({"ssid": b["network"], "bssid": b["bssid"], "security": b["security"],
                          "label": b["security_label"], "reason": b["reason"], "random_bssid": b["random_bssid"],
                          "rssi": _rssi(b),
                          "since": iso(b["first_seen"]), "last_seen": iso(b["last_seen"]),
                          "event_id": b["event_id"]})
        else:
            by_net.setdefault(b["network"], []).append(b)

    managed, friendly = [], []
    for n in configured:
        rows = by_net.get(n["ssid"], [])
        net = seen.get(n["ssid"])
        sensors = {}
        for b in rows:
            for name in (b["rssi"] or {}):
                sensors[name] = sensors.get(name, 0) + 1
        vendors = sorted({b["manuf"] for b in rows if b["manuf"]})
        labels = sorted({b["security_label"] for b in rows})
        base = {"id": n["id"], "ssid": n["ssid"], "bssids": len(rows),
                "bssids_active": sum(1 for b in rows if _active(b, active_after)), "vendors": vendors,
                "sensors": sensors, "note": n["note"], "seen": net is not None}
        if n["kind"] == "managed":
            # BSSIDs without a beacon record are counted, not mixed into the labels, and not assessed
            known = [b for b in rows if _assessed(b)]
            failing = [b for b in known if b["reason"]]
            reasons = sorted({b["reason"] for b in failing})
            managed.append(dict(base, policy=_policy(n),
                                actual={"labels": sorted({b["security_label"] for b in known}),
                                        "mfp": sorted({b["mfp"] or "unknown" for b in known}),
                                        "unknown": len(rows) - len(known)},
                                compliance={"met": len(known) > 0 and not failing, "failing": len(failing),
                                            "total": len(known), "not_assessed": len(rows) - len(known),
                                            "reason": reasons[0] if reasons else None},
                                clients_now=sum(b["clients"] or 0 for b in rows if _active(b, active_after)),
                                bssid_rows=[_bssid_row(b, active_after) for b in rows]))
        else:
            rssi = {}
            for b in rows:
                for name, v in _rssi(b).items():
                    if v is not None:
                        rssi[name] = max(rssi.get(name, v), v)       # the strongest BSSID per sensor
            friendly.append(dict(base, security={"labels": labels, "weakest": (seen.get(n["ssid"]) or {}).get("weakest")},
                                 rssi=rssi))

    hot_bssids = sum(n["bssids"] for n in hotspots)
    hot_sightings = [x for n in hotspots for i in n["ap_ids"] for x in sight.get(i, ())]
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc))
    body.update({
        "collecting_hours": collecting,
        "rogue": rogue,
        "managed": managed,
        "friendly": friendly,
        "other": {
            "networks": len(others), "hidden": sum(1 for n in others if n["hidden"]),
            "hotspots": {"networks": len(hotspots), "bssids": hot_bssids,
                         "median_sighting_s": shape.sighting_median(hot_sightings),
                         # randomized BSSIDs are classified ignored by the rules
                         "ignored": acc["ignore_random_bssid"]},
            "total_rows": len(listed), "page": other_page, "page_size": OTHER_PAGE,
            "rows": [{"network": None if n["hidden"] else n["network_key"], "key": n["network_key"],
                      "hidden": n["hidden"], "bssids": n["bssids"], "vendors": n["vendors"] or [],
                      "sibling": siblings.get(n["network_key"][7:]) if n["network_key"].startswith("hidden:") else None,
                      "security": {"labels": n["labels"], "weakest": n["weakest"]}, "mobile": n["mobile"],
                      "presence": n["presence"], "hours": n["hours"],
                      "median_sighting_s": (shape.sighting_median([x for i in n["ap_ids"] for x in sight.get(i, ())])
                                            if n["presence"] == "transient" else None),
                      "last_seen": iso(n["last_seen"])}
                     for n in page],
        },
        "security_changes": [dict(c, ts=iso(c["ts"])) for c in shape.group_changes(changes)],
    })
    return deps.respond(body, tag)


@router.get("/networks/timeseries")
def networks_timeseries(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
                        range_: str = Depends(deps.range_query)):
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch, envelope=p.envelope(sel["filter"]))
    if cached:
        return cached
    a = _period_args(acc, sel, p)
    with deps.connection() as conn:
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=sel["ids"], **{"from": p.start, "to": p.ends[-1]})
        ts = deps.run(conn, _Q, "timeseries", starts=list(p.starts),
                      **{k: a[k] for k in ("account_id", "sensor_ids", "account_latest", "from", "to")})
    n = len(p.starts)
    buckets, on = shape.buckets(p, shape.coverage(p, hours, sel["sensors"]))
    col = lambda k: shape.series(shape.by_bucket(ts, n, k), on)  # noqa: E731
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc), buckets=buckets)
    body.update({
        "categories": {"configured": col("configured"), "mobile": col("mobile"), "other": col("other")},
        "security": {k: col(k) for k in ("open", "legacy", "psk_no_mfp", "strong", "unknown")},
    })
    return deps.respond(body, tag)


@router.get("/clients/heatmap")
def clients_heatmap(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
                    range_: str = Depends(deps.range_query),
                    view: str = Query("days", pattern="^(days|week)$")):
    if view == "week" and range_ == "24h":
        raise HTTPException(status_code=422, detail="the average week needs a period of at least 7 days")
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch, envelope=p.envelope(sel["filter"]))
    if cached:
        return cached
    p30 = periods.period("30d", acc["tz"], p.now)
    with deps.connection() as conn:
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=sel["ids"], **{"from": p.start, "to": p.ends[-1]})
        rows = deps.run(conn, _Q, "clients_hourly", account_id=acc["id"], sensor_ids=sel["ids"],
                        **{"from": p.start, "to": p.ends[-1]})
        # one scale for every period, view and sensor filter: the account's 30-day peak
        peak30 = deps.run(conn, _Q, "clients_peak", account_id=acc["id"],
                          sensor_ids=[s["id"] for s in acc["sensors"]],
                          **{"from": p30.start, "to": p30.ends[-1]})[0]["peak"]
    thresholds, top = shape.ramp(peak30)
    clients, load_hours = {}, set()
    for r in rows:
        if r["network"] is None:
            load_hours.add(r["hour"])
            continue
        c = clients.setdefault(r["hour"], {"total": 0, "by_network": {}})
        c["total"] += r["clients"] or 0
        c["by_network"][r["network"]] = r["clients"]
    grid, peak = shape.heatmap(p, acc["tz"], clients, load_hours, shape.captured_hours(hours), view)
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc))
    body.update({"view": view, "rows": grid, "peak": peak, "ramp": list(thresholds),
                 "scale": {"peak_30d": peak30, "top": top}})
    return deps.respond(body, tag)
