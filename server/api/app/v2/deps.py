"""FastAPI dependencies and helpers of the v2 endpoints.

The account comes from one place (get_account(): account 1 until login,
PLAN.md 11.4). Filters: sensor=all|<name> (default all), range=24h|7d|30d
(default 7d); an unknown value is a 422. Every response is built in Python
from rows and serialized with json.dumps (timestamps as UTC ISO 8601 with a
Z), guarded by the weak v2 ETag (cache.v2_etag): a matching If-None-Match is
answered with 304, a body this URL got under the same tag is served from
memory (respcache), both before the endpoint's queries run.
"""

from contextlib import contextmanager
from typing import Literal

from fastapi import HTTPException, Query, Request, Response

from .. import cache, db
from . import periods, privacy, queries, respcache, shape

ACCOUNT_ID = 1

Range = Literal["24h", "7d", "30d"]

_Q = queries.load("context")


@contextmanager
def connection():
    """A pooled connection for one request. JIT off: the planner's estimates
    of the period aggregates pass jit_above_cost, and compiling them took 1.3 s
    of the e2-micro's CPU for a query that runs in 0.2 s (2026-10-11)."""
    with db.pool.connection() as conn:
        conn.execute("SET LOCAL jit = off")
        yield conn


def run(conn, group_queries, query, /, **params):
    """Rows of a named query; positional-only, so any name is free for the SQL
    parameters. Never prepared: psycopg prepares a statement after its 5th
    run on a connection, and PostgreSQL may then switch to a generic plan that
    does not see the period arrays and bounds; a custom plan costs 0.3-5 ms
    of planning here (tests/perf/ep_v2.py measures exactly these plans)."""
    return conn.execute(group_queries[query], params, prepare=False).fetchall()


def get_account():
    """The account, its sensors (id, name, first/last poll, last capture) and
    the versions of its ETags. 503 before the first refresh_rollups()."""
    with connection() as conn:
        acc = conn.execute(_Q["account"], {"account_id": ACCOUNT_ID}, prepare=False).fetchone()
    if acc is None:
        raise HTTPException(status_code=503, detail="account not seeded (schema 9)")
    if acc["refreshed_at"] is None:
        raise HTTPException(status_code=503,
                            detail="dashboard tables not built yet: SELECT * FROM refresh_rollups(<sensor id>)")
    for s in acc["sensors"]:
        for k in ("first_poll", "last_poll", "last_capture_at"):
            s[k] = shape.parse_ts(s[k])
    return acc


def select_sensors(acc, sensor):
    """The sensors a request reads: all of the account's, or the named one (else 422)."""
    if sensor == "all":
        chosen = list(acc["sensors"])
    else:
        chosen = [s for s in acc["sensors"] if s["name"] == sensor]
        if not chosen:
            raise HTTPException(status_code=422, detail="unknown sensor %r" % sensor)
    return {
        "filter": sensor,
        "sensors": chosen,
        "ids": [s["id"] for s in chosen],
        # "now" of the data: the newest collecting poll of the selected
        # sensors / of the account (the mobile flag, as in account_aps)
        "latest": _latest(chosen),
        "account_latest": _latest(acc["sensors"]),
    }


def _latest(sensors):
    caps = [s["last_capture_at"] or s["last_poll"] for s in sensors if s["last_capture_at"] or s["last_poll"]]
    return max(caps) if caps else shape.utc_now()


def sensor_query(sensor: str = Query("all", max_length=64, description="all or a sensor name")):
    return sensor


def range_query(range_: Range = Query("7d", alias="range")):
    return range_


def period_of(acc, range_):
    return periods.period(range_, acc["tz"], shape.utc_now())


def etag(request: Request, acc, epoch, *extra, envelope=None):
    """(tag, a response or None): 304 when the client's copy is current, the
    cached body when this URL was answered under the same tag before
    (respcache), else None - the endpoint runs its queries and respond()s.
    envelope: the period envelope of this request (a period endpoint), so a
    cached body never carries an old request time."""
    value = cache.v2_etag(acc["id"], acc["refreshed_at"], acc["write_version"], acc["events_at"], epoch, *extra)
    tag = respcache.make_tag(value, respcache.request_key(request.url.path, request.query_params.multi_items()),
                             envelope)
    if cache.matches(request.headers.get("if-none-match"), tag):
        return tag, Response(status_code=304, headers=cache.headers(tag))
    body = respcache.lookup(tag)
    if body is not None:
        return tag, Response(content=body, media_type="application/json",
                             headers=dict(cache.headers(tag), **{"X-Cache": "hit"}))
    return tag, None


def minute_epoch():
    """The ETag epoch of status responses: they read "now"."""
    return int(shape.utc_now().timestamp()) // 60


def respond(body, tag):
    """The JSON response (UTC timestamps with a Z), kept in respcache for the next
    request of the same URL under the same tag."""
    return Response(content=respcache.render(body, tag), media_type="application/json",
                    headers=dict(cache.headers(tag), **{"X-Cache": "miss"}))


def allowed_bssids(conn, acc, values):
    """The MACs in values that are BSSIDs of the account's APs - what
    privacy.redact() may leave in a response."""
    macs = sorted(privacy.macs_in(values))
    if not macs:
        return set()
    return {r["bssid"] for r in run(conn, _Q, "bssids", account_id=acc["id"], macs=macs)}


def account_head(acc):
    return {"name": acc["name"], "tz": acc["tz"]}
