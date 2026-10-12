"""FastAPI dependencies and helpers of the v2 endpoints.

The account comes from one place (get_account(): account 1 until login,
PLAN.md 11.4). Filters: sensor=all|<name> (default all), range=24h|7d|30d
(default 7d); an unknown value is a 422. Every response is built in Python
from rows and serialized with json.dumps (timestamps as UTC ISO 8601 with a
Z), guarded by the weak v2 ETag (cache.v2_etag): a matching If-None-Match is
answered with 304, a body this URL got under the same tag is served from
memory (respcache), both before the endpoint's queries run.

Writes (PLAN.md 7.5): require_write() checks the bearer token against
WRITE_TOKEN (auth.py); write_tx() is the one transaction of a write - its
audit_log rows and accounts.write_version + 1, so every ETag of the account
changes with it.
"""

import logging
import os
from contextlib import contextmanager
from typing import Literal

from fastapi import Header, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb

from .. import cache, db
from . import auth, periods, privacy, queries, respcache, shape, writes

log = logging.getLogger("api.write")

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


# --- writes (phase 5) ---------------------------------------------------------------------------

def require_write(request: Request, authorization: str | None = Header(None)):
    """The write token (auth.py): 403 when writes are disabled (no
    WRITE_TOKEN on the server), 401 without the right token - logged with
    the client address (the operator or an intruder, never a bystander)."""
    status = auth.check(authorization, os.environ.get("WRITE_TOKEN", ""))
    if status == auth.DISABLED:
        raise HTTPException(status_code=403, detail="writes are disabled: no WRITE_TOKEN on the server")
    if status != auth.OK:
        log.warning("write token %s: %s %s from %s", status, request.method, request.url.path,
                    request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="write token required",
                            headers={"WWW-Authenticate": "Bearer"})


class Audit:
    """The audit rows of one write transaction (audit_log: action, target, old, new)."""

    def __init__(self):
        self.rows = []

    def __call__(self, action, target, old=None, new=None):
        self.rows.append((action, target, old, new))


@contextmanager
def write_tx(acc):
    """(conn, audit): one transaction; on success its audit rows are
    stored and accounts.write_version goes up by one (every ETag of the
    account changes). Any exception rolls everything back."""
    with db.pool.connection() as conn:
        with conn.transaction():
            conn.execute("SET LOCAL jit = off")
            audit = Audit()
            yield conn, audit
            for action, target, old, new in audit.rows:
                conn.execute("INSERT INTO audit_log (account_id, action, target, old, new) "
                             "VALUES (%s, %s, %s, %s, %s)",
                             (acc["id"], action, target, _jsonb(old), _jsonb(new)))
            conn.execute("UPDATE accounts SET write_version = write_version + 1 WHERE id = %s", (acc["id"],))


def _jsonb(v):
    return None if v is None else Jsonb(v, dumps=lambda o: respcache.dumps(o).decode("utf-8"))


def written(body, status=200):
    """The response of a write: JSON as the reads (UTC with a Z), never cached."""
    if body is None:
        return Response(status_code=status, headers={"Cache-Control": "no-store"})
    return Response(content=respcache.dumps(body), media_type="application/json", status_code=status,
                    headers={"Cache-Control": "no-store"})


def invalid(fn, *args, **kwargs):
    """fn(...) with writes.Invalid as a 422."""
    try:
        return fn(*args, **kwargs)
    except writes.Invalid as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


def refresh_events_all(conn, acc):
    """refresh_events() for every sensor of the account (after a change of
    the networks or the rules); returns {sensor: counts}."""
    out = {}
    for s in acc["sensors"]:
        r = conn.execute("SELECT * FROM refresh_events(%s)", (s["id"],)).fetchone()
        out[s["name"]] = r
    return out
