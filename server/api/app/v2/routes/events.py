"""GET /api/v2/events and /api/v2/events/{id} (Events board).

The feed is newest first, keyset-paged on the rows' (ts, id) with opaque
cursors (before = older page, after = newer page; never OFFSET), 50 rows per
page (at most 100). A row is one event, or repeated info / low events of
one AP, type and status on one local day (events.sql: _row; display only,
item.group lists them). Filters: sensor, status and type (comma-separated),
severity (the lowest shown), managed (the event's network is a managed one),
q (BSSID / SSID substring). counts (events) and rows (feed rows): per status
under every filter but status. No client MAC: summaries and evidence are
redacted to the account's AP BSSIDs (privacy.redact()).
"""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from .. import cursor, deps, privacy, queries, shape
from ..periods import iso

router = APIRouter(prefix="/events", tags=["v2 events"])

_Q = queries.load("events")

STATUSES = ("open", "confirmed", "false_positive")
KINDS = ("detection", "kismet", "config", "sensor")
SEVERITIES = ("info", "low", "medium", "high", "critical")
NO_BEFORE = ("infinity", 2 ** 63 - 1)
NO_AFTER = ("-infinity", 0)


def _csv(value, allowed, name):
    if not value:
        return []
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise HTTPException(status_code=422, detail="%s: unknown value(s) %s" % (name, ", ".join(bad)))
    return items


def _like(q):
    """A substring for LIKE: the wildcards of the input taken literally."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") if q else None


def _cursor(token, name):
    try:
        return cursor.decode(token)
    except ValueError:
        raise HTTPException(status_code=422, detail="%s: invalid cursor" % name) from None


def _item(r, members=()):
    item = {
        "id": r["id"], "ts": iso(r["ts"]), "ts_end": iso(r["ts_end"]), "severity": r["severity"],
        "kind": r["kind"], "type": r["type"], "type_label": r["type_label"], "summary": r["summary"],
        "ap": (None if r["bssid"] is None else
               {"bssid": r["bssid"], "ssid": r["ssid"], "network": r["network"],
                "classification": r["classification"]}),
        "sensors": [r["sensor"]], "status": r["status"], "status_at": iso(r["status_at"]),
    }
    if "members" in r:
        item["group"] = shape.event_group(r, members)
    return item


@router.get("")
def events(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
           status: str | None = Query(None, max_length=64, description="csv: open,confirmed,false_positive"),
           kinds: str | None = Query(None, alias="type", max_length=64,
                                     description="csv: detection,kismet,config,sensor"),
           severity: str | None = Query(None, max_length=16,
                                        description="the lowest severity: info,low,medium,high,critical"),
           managed: bool = Query(False, description="only events on managed networks"),
           q: str | None = Query(None, max_length=64, description="BSSID or SSID substring"),
           before: str | None = Query(None, max_length=64, description="cursor: the page older than it"),
           after: str | None = Query(None, max_length=64, description="cursor: the page newer than it"),
           limit: int = Query(50, ge=1, le=100)):
    sel = deps.select_sensors(acc, sensor)
    statuses = _csv(status, STATUSES, "status")
    kind_list = _csv(kinds, KINDS, "type")
    if severity and severity not in SEVERITIES:
        raise HTTPException(status_code=422, detail="severity: unknown value %s" % severity)
    if before and after:
        raise HTTPException(status_code=422, detail="before and after are exclusive")
    b = _cursor(before, "before") if before else NO_BEFORE
    a = _cursor(after, "after") if after else NO_AFTER
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    filters = {"account_id": acc["id"], "sensor_ids": sel["ids"], "kinds": kind_list,
               "severity": severity or "", "managed": managed, "q": _like(q), "tz": acc["tz"]}
    with deps.connection() as conn:
        counts = deps.run(conn, _Q, "counts", **filters)
        rows = deps.run(conn, _Q, "feed", status=statuses, before_ts=b[0], before_id=b[1],
                        after_ts=a[0], after_id=a[1], oldest_first=bool(after), limit=limit + 1, **filters)
        ids = [i for r in rows for i in (r["members"] or ())]
        members = deps.run(conn, _Q, "members", ids=ids) if ids else []
        allowed = deps.allowed_bssids(conn, acc, [[r["bssid"], r["summary"]] for r in rows]
                                      + [m["summary"] for m in members])
    # one row more than the page: more events in the direction of travel
    more = len(rows) > limit
    if more:
        rows = rows[1:] if after else rows[:limit]
    newer = older = None
    if rows:
        if (after and more) or before:
            newer = cursor.encode(rows[0]["ts"], rows[0]["id"])
        if (not after and more) or after:
            older = cursor.encode(rows[-1]["ts"], rows[-1]["id"])
    elif before:
        newer = before
    elif after:
        older = after
    n_events, n_rows = shape.event_counts(counts, STATUSES)
    body = {
        "filters": {"sensor": sel["filter"], "status": statuses, "type": kind_list, "severity": severity,
                    "managed": managed, "q": q},
        "counts": n_events, "rows": n_rows,
        "items": privacy.redact([_item(r, members) for r in rows], allowed),
        "newer": newer, "older": older, "limit": limit,
    }
    return deps.respond(body, tag)


@router.get("/{event_id}")
def event(request: Request, event_id: int = Path(ge=1, le=2 ** 63 - 1), acc=Depends(deps.get_account)):
    tag, cached = deps.etag(request, acc, None)
    if cached:
        return cached
    with deps.connection() as conn:
        found = deps.run(conn, _Q, "event", account_id=acc["id"], event_id=event_id)
        if not found:
            raise HTTPException(status_code=404, detail="unknown event")
        r = found[0]
        who = []
        if r["ap_id"] is not None:
            ts = r["ts"]
            # the hours of the event; a point event: its hour
            end = r["ts_end"] or ts
            who = deps.run(conn, _Q, "who_saw_it", ap_id=r["ap_id"], ts=ts, ts_end=end)
        allowed = deps.allowed_bssids(conn, acc, [r["bssid"], r["summary"], r["evidence"]])
    item = _item(r)
    item.update({"status_note": r["status_note"], "created_at": iso(r["created_at"]),
                 "updated_at": iso(r["updated_at"])})
    who_saw = [{"sensor": w["sensor"], "seen": w["seen"],
                "rssi_median": None if w["rssi_median"] is None else round(float(w["rssi_median"]), 1),
                "clients": w["clients"], "first_seen": iso(w["first_seen"]), "last_seen": iso(w["last_seen"])}
               for w in who]
    clients = [w["clients"] for w in who if w["clients"] is not None]
    body = dict(item, evidence=r["evidence"], who_saw_it=who_saw, clients=max(clients) if clients else None)
    return deps.respond(privacy.redact(body, allowed), tag)
