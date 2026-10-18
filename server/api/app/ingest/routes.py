"""POST /api/ingest/v1/batch and /api/ingest/v1/heartbeat (docs/live-upload.md 5, 6).

The sensor's uploader sends one batch per poll (a catch-up batch after an
outage), gzip-JSON, `Authorization: Bearer wsi1.<id>.<secret>`; the sensor is
the token's - the payload never names one. A batch is COPYed into the session
staging tables and merged by ingest_merge() (schema.sql, shared with the
snapshot import) in one transaction, together with the upload record, the
clock corrections, the gaps, the sensor's status (contact, immediate) and the
heartbeat it carries. A standalone heartbeat comes when no batch was sent for
a minute. The dashboard's rollups are not refreshed here: ingest_merge()
marks the sensor dirty and the refresher (app/refresher.py) does it.

Status codes: 200; 400 body not gzip/JSON; 401 no or wrong token; 403 token
revoked; 409 a batch of this sensor (or a snapshot import) is running; 413 too
large; 422 content invalid (the first problem named); 429 rate limit
(Retry-After); 503 ingest disabled (INGEST_ENABLED=0) or the database
unreachable. Every decision is in the stdlib modules next to this one
(validate, tokens, ratelimit, stalls, heartbeat - unit-tested); this file only
sequences them and the SQL (sql/ingest.sql).
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg import errors
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from .. import db
from ..v2 import queries
from . import heartbeat as hbmod
from . import ratelimit, stalls, tokens, validate

log = logging.getLogger("api.ingest")

router = APIRouter(prefix="/api/ingest/v1", tags=["ingest"])

with open(os.path.join(os.path.dirname(__file__), "sql", "ingest.sql"), encoding="utf-8") as _f:
    _Q = queries.parse(_f.read())

BATCHES = ratelimit.Buckets(per_minute=120, burst=20)     # steady 2/min, catch-up ~25/min
HEARTBEATS = ratelimit.Buckets(per_minute=10, burst=5)
FAILS = ratelimit.FailWindow(limit=20, window_s=60, block_s=600)
MERGE = threading.Semaphore(1)       # one merge at a time: the dashboard keeps 3 of the 4 pool connections
MERGE_WAIT_S = 20


def _enabled():
    return os.environ.get("INGEST_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def _run(conn, query, **params):
    return conn.execute(_Q[query], params, prepare=False)


def _err(status, detail, retry_after=None):
    headers = {"Retry-After": str(max(1, int(retry_after + 0.999)))} if retry_after else None
    if status == 401:
        headers = dict(headers or {}, **{"WWW-Authenticate": "Bearer"})
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


def _dt(x):
    """unix seconds -> aware datetime, None for None or a value out of range."""
    if x is None:
        return None
    try:
        return datetime.fromtimestamp(x, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


async def _read(request):
    """The request body, never more than validate.MAX_BODY bytes."""
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > validate.MAX_BODY:
        raise validate.TooLarge("body larger than %d bytes" % validate.MAX_BODY)
    chunks, n = [], 0
    async for chunk in request.stream():
        n += len(chunk)
        if n > validate.MAX_BODY:
            raise validate.TooLarge("body larger than %d bytes" % validate.MAX_BODY)
        chunks.append(chunk)
    return b"".join(chunks)


# --- authentication ------------------------------------------------------------------------------

def _authenticate(conn, authorization, client):
    """(None, token row) or (error response, None). A wrong secret of a known
    token, or a revoked token, counts towards the upload_unauthorized stall of
    its sensor; every failure counts towards the client address's block."""
    mono = time.monotonic()
    token = tokens.from_header(authorization)
    token_id = tokens.parse(token)
    row = None
    if token_id is not None:
        with conn.transaction():
            row = _run(conn, "token", token_id=token_id).fetchone()
    if row is None:
        FAILS.failed(client, mono)
        log.warning("ingest: unknown or malformed token from %s", client)
        return _err(401, "ingest token required"), None
    if not tokens.matches(token, row["token_hash"]):
        FAILS.failed(client, mono)
        log.warning("ingest: wrong secret for %s (sensor %s) from %s", tokens.describe(token), row["sensor"], client)
        _auth_failed(conn, row)
        return _err(401, "ingest token required"), None
    if row["revoked_at"] is not None:
        log.warning("ingest: revoked %s (sensor %s) from %s", tokens.describe(token), row["sensor"], client)
        _auth_failed(conn, row)
        return _err(403, "ingest token revoked"), None
    return None, row


def _auth_failed(conn, tok):
    now = datetime.now(timezone.utc)
    with conn.transaction():
        st = _run(conn, "status_get", sensor_id=tok["sensor_id"]).fetchone()
        if st is None:
            return                      # never had contact: nothing to track yet
        new, open_ = stalls.after_auth_fail(st, now)
        _run(conn, "stall_write", sensor_id=tok["sensor_id"], **_stall_params(new))
        if open_:
            _open_event(conn, tok, "upload_unauthorized", new, now)


def _stall_params(st):
    return {k: st[k] for k in ("reject_seq", "reject_since", "reject_count", "reject_reason",
                               "auth_fail_since", "auth_fail_count")}


def _open_event(conn, tok, kind, st, now, **kw):
    summary, evidence = stalls.event(kind, st, **kw)
    if _run(conn, "event_open", sensor_id=tok["sensor_id"], ts=now, type=kind, summary=summary,
            evidence=Jsonb(evidence)).fetchone():
        log.warning("ingest: sensor %s - %s opened", tok["sensor"], kind)


def _close_events(conn, tok, kinds):
    for r in _run(conn, "event_close", sensor_id=tok["sensor_id"], types=list(kinds)).fetchall():
        log.info("ingest: sensor %s - %s closed (event %s)", tok["sensor"], r["type"], r["id"])


# --- the heartbeat's effects (both routes) ---------------------------------------------------------

def _heartbeat(conn, tok, hb, now):
    """History row (at most one a minute), the adapter check, the retention event."""
    if hb is None:
        return
    sid = tok["sensor_id"]
    last = _run(conn, "heartbeat_last", sensor_id=sid).fetchone()["last"]
    if hbmod.history_due(last, now):
        _run(conn, "heartbeat_insert", sensor_id=sid, received_at=now, sensor_ts=_dt(hb["sensor_ts"]),
             rtt_ms=hb["rtt_ms"], clock_offset_s=hbmod.clock_offset_s(now.timestamp(), hb["sensor_ts"], hb["rtt_ms"]),
             chrony_synced=hb["chrony_synced"], chrony_offset_s=hb["chrony_offset_s"], boot_id=hb["boot_id"],
             uptime_s=None if hb["uptime_s"] is None else int(hb["uptime_s"]),
             last_poll_ts=_dt(hb["last_poll_ts"]), backlog_polls=hb["backlog_polls"],
             backlog_oldest_ts=_dt(hb["backlog_oldest_ts"]), held_polls=hb["held_polls"],
             unverified_polls=hb["unverified_polls"], buffer_oldest_ts=_dt(hb["buffer_oldest_ts"]),
             buffer_bytes=hb["buffer_bytes"], disk_free_bytes=hb["disk_free_bytes"],
             uploader_version=hb["uploader_version"])
    _adapter(conn, tok, hb.get("adapter"), now)
    newest = _run(conn, "resume", sensor_id=sid).fetchone()
    if hbmod.retention_risk(hb, now, None if newest is None else newest["ts"]):
        _open_event(conn, tok, "upload_retention_risk", {}, now, oldest=hb["backlog_oldest_ts"],
                    retention_days=hb["retention_days"])
    else:
        _close_events(conn, tok, ("upload_retention_risk",))


def _adapter(conn, tok, reported, now):
    sid = tok["sensor_id"]
    stored = _run(conn, "adapter_get", sensor_id=sid).fetchone()
    action = hbmod.adapter_action(stored, reported)
    if action == "none":
        return
    name = "%s USB adapter" % reported["chipset"] if reported.get("chipset") else "Capture adapter"
    params = dict(sensor_id=sid, name=(stored or {}).get("name") or name, usb_id=reported.get("usb_id"),
                  mac=reported.get("mac"), chipset=reported.get("chipset"), driver=reported.get("driver"),
                  interface=reported.get("interface"))
    if action == "fill":
        _run(conn, "adapter_fill", **params)
        log.info("ingest: sensor %s reported its adapter %s (identity filled, no boundary)",
                 tok["sensor"], reported.get("usb_id"))
        return
    params["name"] = name
    _run(conn, "adapter_change", **params)
    old = {"usb_id": stored.get("usb_id"), "mac": stored.get("mac")}
    new = {"usb_id": reported.get("usb_id"), "mac": reported.get("mac"), "chipset": reported.get("chipset")}
    label = "Adapter changed: %s -> %s" % (old["usb_id"] or "?", new["usb_id"] or "?")
    _run(conn, "hardware_boundary", sensor_id=sid, at=now, label=label)
    _run(conn, "audit", account_id=tok["account_id"], action="sensor.adapter", target="sensor:%s" % tok["sensor"],
         old=Jsonb(old), new=Jsonb(new))
    _run(conn, "write_version", account_id=tok["account_id"])
    log.warning("ingest: sensor %s - %s (a hardware_change boundary)", tok["sensor"], label)


def _status(conn, tok, st, now, hb=None, batch=None):
    """Write the sensor's status row: contact now, the stall fields of st."""
    old = dict(st or {})
    _run(conn, "status_write", sensor_id=tok["sensor_id"], last_contact_at=now,
         last_batch_at=now if batch else old.get("last_batch_at"),
         last_batch_seq=batch["batch_seq"] if batch else old.get("last_batch_seq"),
         buffer_id=batch["buffer_id"] if batch else (str(old["buffer_id"]) if old.get("buffer_id") else None),
         heartbeat=Jsonb(hb) if hb is not None else Jsonb(old.get("heartbeat") or {}),
         **_stall_params(st if st else stalls.clear()))
    _run(conn, "token_used", token_id=tok["id"])


# --- routes ------------------------------------------------------------------------------------------

@router.post("/batch")
async def batch(request: Request):
    if not _enabled():
        return _err(503, "ingest disabled on this server", 60)
    client = request.client.host if request.client else "?"
    blocked = FAILS.blocked(client, time.monotonic())
    if blocked:
        return _err(429, "too many failed authentications", blocked)
    try:
        raw = await _read(request)
    except validate.TooLarge as e:
        return _err(413, str(e))
    return await run_in_threadpool(_batch, raw, request.headers.get("content-encoding"),
                                   request.headers.get("authorization"), client)


def _batch(raw, encoding, authorization, client):
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)
    with db.pool.connection() as conn:
        error, tok = _authenticate(conn, authorization, client)
        if error:
            return error
        allowed, retry = BATCHES.allow(tok["sensor_id"], time.monotonic())
        if not allowed:
            return _err(429, "rate limit", retry)
        try:
            body = validate.decode_body(raw, encoding)
        except validate.BadRequest as e:
            return _err(400, str(e))
        except validate.TooLarge as e:
            return _err(413, str(e))
        try:
            b = validate.batch(body, now.timestamp())
        except validate.Invalid as e:
            _rejected(conn, tok, body.get("batch_seq"), str(e), now)
            return _err(422, str(e))
        if not MERGE.acquire(timeout=MERGE_WAIT_S):
            return _err(429, "another sensor's batch is being merged", 5)
        try:
            return _merge(conn, tok, b, len(raw), now, t0)
        except errors.LockNotAvailable:
            return _err(409, "a snapshot import of this sensor is running", 30)
        finally:
            MERGE.release()


def _rejected(conn, tok, batch_seq, reason, now):
    log.warning("ingest: sensor %s batch %s refused: %s", tok["sensor"], batch_seq, reason)
    with conn.transaction():
        st = _run(conn, "status_get", sensor_id=tok["sensor_id"]).fetchone()
        if st is None:
            return
        new, open_ = stalls.after_reject(st, batch_seq if isinstance(batch_seq, int) else None, reason, now)
        _run(conn, "stall_write", sensor_id=tok["sensor_id"], **_stall_params(new))
        if open_:
            _open_event(conn, tok, "upload_rejected", new, now)


def _merge(conn, tok, b, nbytes, now, t0):
    sid = tok["sensor_id"]
    with conn.transaction():
        conn.execute("SET LOCAL jit = off")
        conn.execute("SET LOCAL lock_timeout = '20s'")
        conn.execute("SET LOCAL statement_timeout = '120s'")
        if not _run(conn, "try_lock", sensor_id=sid).fetchone()["locked"]:
            return _err(409, "a batch of this sensor is being merged", 2)
        st = _run(conn, "status_get", sensor_id=sid).fetchone()
        _run(conn, "stage_create")
        with conn.cursor() as cur:
            for name in validate.ORDER:
                rows = b["tables"].get(name)
                if not rows:
                    continue
                with cur.copy("COPY stage_%s (%s) FROM STDIN" % (name, ", ".join(validate.columns(name)))) as cp:
                    for row in rows:
                        cp.write_row(row)
        merged = _run(conn, "merge", sensor_id=sid).fetchone()["merged"]
        ms = int((time.monotonic() - t0) * 1000)
        replay = _run(conn, "upload", sensor_id=sid, buffer_version=b["buffer_version"],
                      polls_from=b["polls_from"], polls_to=b["polls_to"],
                      buffer_bytes=(b["heartbeat"] or {}).get("buffer_bytes"),
                      rows=Jsonb(merged.get("inserted") or {}), buffer_id=b["buffer_id"],
                      batch_seq=b["batch_seq"], sent_at=b["sent_at"], attempt=b["attempt"], bytes=nbytes,
                      duration_ms=ms).fetchone()["replay"]
        for boot in b["boots"]:
            _run(conn, "boot_upsert", sensor_id=sid, **boot)
        _gaps(conn, tok, b)
        new = stalls.after_accept(st)
        _close_events(conn, tok, stalls.CLOSE_ON_ACCEPT)
        _heartbeat(conn, tok, b["heartbeat"], now)
        _status(conn, tok, new, now, hb=b["heartbeat"], batch=b)
    polls = len(b["tables"].get("polls", []))
    log.info("ingest: sensor %s batch %d%s: %d polls, %d rows, inserted %s, %d ms",
             tok["sensor"], b["batch_seq"], " (replay)" if replay else "", polls, b["rows"],
             merged.get("inserted"), ms)
    return JSONResponse({"batch_seq": b["batch_seq"], "replay": bool(replay), "staged": merged.get("staged"),
                         "inserted": merged.get("inserted"), "conflicts": merged.get("conflicts"), "ms": ms,
                         "server_ts": datetime.now(timezone.utc).timestamp()})


def _gaps(conn, tok, b):
    """Polls that never arrived (sensor_seq_gaps): lost (pruned on the sensor
    before the upload - also a 'lost' annotation on the time line) or
    unverified (a boot that ended before its clock anchor; counted only - its
    time is unknown)."""
    if not b["gaps"]:
        return
    seq_i, ts_i = validate.columns("polls").index("seq"), validate.columns("polls").index("ts")
    by_seq = {p[seq_i]: p[ts_i] for p in b["tables"].get("polls", [])}
    for g in b["gaps"]:
        stored = _run(conn, "gap_insert", sensor_id=tok["sensor_id"], buffer_id=b["buffer_id"], **g).fetchone()
        if stored is None or g["reason"] != "lost":
            continue
        _run(conn, "lost_annotation", sensor_id=tok["sensor_id"], seq_from=g["seq_from"],
             after_ts=_dt(by_seq.get(g["seq_to"] + 1)),
             label="%d poll(s) lost: pruned on the sensor before the upload" % g["polls"])
        log.warning("ingest: sensor %s - %d polls lost (seq %d-%d)", tok["sensor"], g["polls"],
                    g["seq_from"], g["seq_to"])


@router.post("/heartbeat")
async def heartbeat(request: Request):
    if not _enabled():
        return _err(503, "ingest disabled on this server", 60)
    client = request.client.host if request.client else "?"
    blocked = FAILS.blocked(client, time.monotonic())
    if blocked:
        return _err(429, "too many failed authentications", blocked)
    try:
        raw = await _read(request)
    except validate.TooLarge as e:
        return _err(413, str(e))
    return await run_in_threadpool(_heartbeat_route, raw, request.headers.get("content-encoding"),
                                   request.headers.get("authorization"), client)


def _heartbeat_route(raw, encoding, authorization, client):
    now = datetime.now(timezone.utc)
    with db.pool.connection() as conn:
        error, tok = _authenticate(conn, authorization, client)
        if error:
            return error
        allowed, retry = HEARTBEATS.allow(tok["sensor_id"], time.monotonic())
        if not allowed:
            return _err(429, "rate limit", retry)
        try:
            body = validate.decode_body(raw, encoding)
            if body.get("v") != 1:
                raise validate.Invalid("unsupported payload version %r" % body.get("v"))
            hb = validate.heartbeat(body.get("heartbeat"))
        except validate.BadRequest as e:
            return _err(400, str(e))
        except validate.TooLarge as e:
            return _err(413, str(e))
        except validate.Invalid as e:
            return _err(422, str(e))
        with conn.transaction():
            conn.execute("SET LOCAL jit = off")
            st = _run(conn, "status_get", sensor_id=tok["sensor_id"]).fetchone()
            new = stalls.after_auth_ok(st)
            _close_events(conn, tok, ("upload_unauthorized",))
            _heartbeat(conn, tok, hb, now)
            _status(conn, tok, new, now, hb=hb)
            resume = _run(conn, "resume", sensor_id=tok["sensor_id"]).fetchone()
    return JSONResponse({"server_ts": datetime.now(timezone.utc).timestamp(),
                         "resume": None if resume is None else {"seq": resume["seq"], "ts": resume["ts"]},
                         "last_batch_seq": (st or {}).get("last_batch_seq"),
                         "buffer_id": str(st["buffer_id"]) if st and st.get("buffer_id") else None})
