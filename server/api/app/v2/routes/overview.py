"""GET /api/v2/overview/status and /api/v2/overview/period (Main board).

status (now, sensor filter): open events by severity, managed networks
compliant m / n with the first problem, "Needs attention" (open events of
severity medium or higher + compliance findings). period (sensor + range):
the period envelope with the buckets and their coverage per sensor, the three
tiles (new APs, open networks nearby, clients on managed APs), APs over time
(fixed / mobile, events per bucket) and the security of the APs seen.
Series are aligned with the buckets; null = no selected sensor collecting.
"""

from fastapi import APIRouter, Depends, Request

from .. import deps, queries, shape
from ..periods import iso

router = APIRouter(prefix="/overview", tags=["v2 overview"])

_Q = queries.load("overview")
_QC = queries.load("context")

AVG_LABEL = {"24h": "hourly average", "7d": "average 6-hour peak", "30d": "average daily peak"}
EVENTS_PER_BUCKET = 5


@router.get("/status")
def status(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query)):
    sel = deps.select_sensors(acc, sensor)
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        st = deps.run(conn, _Q, "status", account_id=acc["id"], sensor_ids=sel["ids"], latest=sel["latest"])[0]
        events = deps.run(conn, _Q, "attention", sensor_ids=sel["ids"])
    summary, findings = shape.compliance(st["networks"])
    for e in events:
        e["ts"], e["ts_end"] = iso(e["ts"]), iso(e["ts_end"])
    items, total = shape.attention(events, findings)
    body = {
        "account": deps.account_head(acc),
        "filters": {"sensor": sel["filter"]},
        "open_events": {"total": st["open_total"], "by_severity": shape.severity_counts(st["open_by_severity"])},
        "compliance": dict(summary, networks=st["networks"]),
        "attention": items,
        "attention_total": total,
    }
    return deps.respond(body, tag)


@router.get("/period")
def period(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
           range_: str = Depends(deps.range_query)):
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch)
    if cached:
        return cached
    args = {"account_id": acc["id"], "sensor_ids": sel["ids"], "account_latest": sel["account_latest"],
            "starts": list(p.starts), "from": p.start, "to": p.ends[-1]}
    with deps.connection() as conn:
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=sel["ids"], **{"from": p.start, "to": p.ends[-1]})
        pres = deps.run(conn, _Q, "presence", **args)[0]
        clients = deps.run(conn, _Q, "clients", **{k: args[k] for k in ("account_id", "sensor_ids", "from", "to")})
        events = deps.run(conn, _Q, "events", **{k: args[k] for k in ("sensor_ids", "starts", "from", "to")})

    n = len(p.starts)
    cov = shape.coverage(p, hours, sel["sensors"])
    buckets, on = shape.buckets(p, cov)
    fixed = shape.by_bucket(pres["buckets"], n, "fixed")
    mobile = shape.by_bucket(pres["buckets"], n, "mobile")
    open_ = shape.by_bucket(pres["buckets"], n, "open")
    new = shape.by_bucket(pres["new_aps"], n, "new")
    new_mobile = shape.by_bucket(pres["new_aps"], n, "mobile")

    peak, load_share, (cpeak, cpeak_at) = shape.clients_by_bucket(p, clients, shape.captured_hours(hours))
    cseries = shape.series(peak, on)
    for i, b in enumerate(buckets):
        # captured, but no load rows (2026-10-03 11:00 until collector v7):
        # "client counts were not recorded" (hatched)
        b["load_missing"] = bool(on[i] and load_share[i] == 0)
    with_data = [v for v in cseries if v is not None]

    per_bucket = [[] for _ in range(n)]
    counts = [0] * n
    for e in events:
        i = e["i"] - 1
        counts[i] += 1
        if len(per_bucket[i]) < EVENTS_PER_BUCKET:
            per_bucket[i].append({"id": e["id"], "severity": e["severity"], "kind": e["kind"], "title": e["summary"]})

    total_new = sum(new)
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc), buckets=buckets)
    body.update({
        "tiles": {
            "new_aps": {"total": total_new,
                        "mobile_share": round(sum(new_mobile) / total_new, 3) if total_new else None,
                        "series": shape.series(new, on)},
            "open_nearby": {"total": pres["open_seen"], "aps_seen": pres["aps_seen"],
                            "share": round(pres["open_seen"] / pres["aps_seen"], 3) if pres["aps_seen"] else None,
                            "series": shape.series(open_, on)},
            "clients_managed": {"peak": cpeak, "peak_at": iso(cpeak_at),
                                "avg_label": AVG_LABEL[p.range],
                                "avg": round(sum(with_data) / len(with_data), 1) if with_data else None,
                                "series": cseries, "load_share": load_share},
        },
        "aps": {"fixed": shape.series(fixed, on), "mobile": shape.series(mobile, on),
                "events": per_bucket, "event_counts": counts},
        "security": shape.security_rows(pres["security"]),
    })
    return deps.respond(body, tag)
