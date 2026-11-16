"""The refresher (docs/live-upload.md 6.4): `python -m app.refresher`, the
compose service `refresher` (the api image, its own container - a slow
refresh never holds one of the api's pool connections).

Live ingest marks a sensor dirty (ingest_state.dirty_from = the earliest ts
it inserted; ingest_merge()) instead of refreshing per request. Each round:

0. discard mode's auto-expiry (schema 20, docs/discard-mode.md):
   discard_close_expired() records the end of every window past its `until`
   (the ingest stops discarding at `until` by itself; this writes the history
   entry's end, the audit_log row and write_version + 1, so the dashboard's
   banner goes);
1. per dirty sensor: take the mark - one statement moves it from dirty_from
   to taken_from (schema 21) - then refresh_rollups(sensor, dirty_from), then
   clear taken_from; an ingest during the refresh sets a new mark for the next
   round (only its own new rows). A refresh that did not finish - it failed,
   its connection died (Postgres restarting after a crash) or this process
   died with it (the stack restarted, the box rebooted: until schema 21 the
   taken mark lived in this process's memory only) - leaves its mark in
   taken_from, and every round first puts such marks back into dirty_from
   (earliest wins), so the window it was refreshing is never lost. (Clearing
   only "if no ingest moved it meanwhile" never cleared it with a batch every
   30 s and rounds longer than that: every round re-refreshed days of hours -
   found on the first live day.)
2. refresh_contact_periods() for every sensor that ever uploaded
   ('unreachable' annotations from heartbeat gaps);
3. the e-mail notifications (app/notify.py: a sensor without contact or
   capture, a disk running low; off without NOTIFY_SMTP_HOST / NOTIFY_TO);
4. once a night at 03:30 in the account's time zone, refresh_ap_baselines();
5. once a night between 04:00 and 06:00 local time, the batch detectors
   (REFRESH_DETECTORS, default deauth-flood,evil-twin; empty = off) over
   the last DETECT_WINDOW_H (48) hours of each sensor, each its own
   `python -m detection` process (server/detection/ is mounted at
   /app/detection): idempotent re-runs that write only `detections` and
   never touch an operator's label (detection/README.md). Since 2026-11-16;
   before, the detectors ran only by hand.

The next round starts no earlier than max(REFRESH_INTERVAL_S, 4 x the last
round's duration) after this one started (refresh_schedule.next_delay(): at
most a fifth of the e2-micro's core, however slow a refresh gets). The
duration excludes the nightly baselines: one ~5-11 min run would otherwise
hold the next rollup round back by four times that (found on the first
night). Every
round logs one line per step with its duration - the measurement the
interval is chosen from (N8) - and writes the same as one refresh_runs row
per step (schema 16; steps "rollups", "contact_periods", "ap_baselines" and
"round"), the series of the system evaluation (evaluation/pipeline/). A
missing table (schema 15) or a failed insert never stops a round. Statements run in autocommit: each function is
its own transaction (refresh_rollups() takes its own per-sensor lock).
"""

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta

import psycopg
from psycopg.rows import dict_row

from . import notify
from . import refresh_schedule as rs

log = logging.getLogger("refresher")

INTERVAL_S = float(os.environ.get("REFRESH_INTERVAL_S", "60"))
BASELINES = os.environ.get("REFRESH_BASELINES", "1").strip() not in ("0", "false", "no", "off")
RETRY_S = 30
DETECTORS = [d.strip() for d in os.environ.get("REFRESH_DETECTORS", "deauth-flood,evil-twin").split(",") if d.strip()]
DETECT_WINDOW_H = float(os.environ.get("DETECT_WINDOW_H", "48"))
DETECT_TIMEOUT_S = 1800
DETECT_DIR = os.environ.get("DETECT_DIR", "/app")       # holds detection/ (the compose mount)

_stop = False
_recording = True
_expiry = True  # discard_close_expired() exists (schema 20)

_notifier = notify.Notifier(None)   # set up in main()

_RUN_COLS = ("refresh_from", "waited_s", "ap_hours", "sensor_hours", "gaps", "baselines", "next_in_s", "error")

# The dirty mark's three statements (schema 21). tests/sql/refresher_marks_test.sql
# runs these very texts on the server (tests/test_schema_files.py compares them).
SQL_RESTORE_TAKEN = (
    "UPDATE ingest_state SET dirty_from = least(dirty_from, taken_from), "
    "dirty_since = least(dirty_since, taken_since), taken_from = NULL, taken_since = NULL "
    "WHERE taken_from IS NOT NULL RETURNING sensor_id, dirty_from")
SQL_TAKE_MARK = (
    "UPDATE ingest_state i SET taken_from = least(i.taken_from, o.dirty_from), "
    "taken_since = least(i.taken_since, o.dirty_since), dirty_from = NULL, dirty_since = NULL "
    "FROM (SELECT sensor_id, dirty_from, dirty_since FROM ingest_state "
    "WHERE sensor_id = %s AND dirty_from IS NOT NULL FOR UPDATE) o "
    "WHERE i.sensor_id = o.sensor_id "
    "RETURNING i.taken_from AS dirty_from, i.taken_since AS dirty_since")
SQL_MARK_DONE = "UPDATE ingest_state SET taken_from = NULL, taken_since = NULL WHERE sensor_id = %s"


def record(conn, step, started_at, duration_s, sensor_id=None, ok=True, **cols):
    """One refresh_runs row; never raises (a log row must not stop a refresh)."""
    global _recording
    if not _recording or conn is None or conn.closed:
        return
    vals = [cols.get(c) for c in _RUN_COLS]
    try:
        conn.execute("INSERT INTO refresh_runs (started_at, step, sensor_id, duration_s, ok, %s) "
                     "VALUES (%%s, %%s, %%s, %%s, %%s, %s)" % (", ".join(_RUN_COLS), ", ".join(["%s"] * len(vals))),
                     (started_at, step, sensor_id, round(duration_s, 3), ok, *vals), prepare=False)
    except psycopg.errors.UndefinedTable:
        _recording = False
        log.warning("refresh_runs missing (schema < 16): steps are only logged")
    except psycopg.Error as e:
        log.warning("refresh_runs: row not written: %s", e)


def restore_taken(conn):
    """Marks a refresh took and did not finish (ingest_state.taken_from, schema
    21) go back into dirty_from - the earliest wins. Returns how many."""
    rows = conn.execute(SQL_RESTORE_TAKEN, prepare=False).fetchall()
    for r in rows:
        log.warning("sensor_id=%s: the mark of a refresh that did not finish is back, from=%s", r["sensor_id"],
                    r["dirty_from"].isoformat())
    return len(rows)


def close_expired(conn):
    """Step 0: discard windows past their end are recorded as ended. Turns
    itself off on a database before schema 20 (no such function)."""
    global _expiry
    if not _expiry:
        return
    try:
        rows = conn.execute("SELECT * FROM discard_close_expired()", prepare=False).fetchall()
    except psycopg.errors.UndefinedFunction:
        _expiry = False
        log.warning("discard_close_expired() missing (schema < 20): no auto-expiry step")
        return
    for r in rows:
        log.info("discard mode ended (expiry) sensor=%s window=%s until=%s", r["sensor"], r["window_id"],
                 r["until"].isoformat())


def run_detector(name, sensor, frm, to, timeout=DETECT_TIMEOUT_S):
    """One detector as its own process; returns (ok, seconds, summary lines).
    Only the detector's summary lines are logged (its table names APs)."""
    cmd = [sys.executable, "-m", "detection", name, "--sensor", sensor,
           "--from", frm.isoformat(), "--to", to.isoformat()]
    t = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=DETECT_DIR, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, time.monotonic() - t, [str(e)[:300]]
    lines = [ln.strip() for ln in (r.stdout + r.stderr).splitlines()
             if ln.strip().startswith(("scanned", "peak RSS", "eligibility"))]
    if r.returncode != 0:
        lines.append((r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1][:300])
    return r.returncode == 0, time.monotonic() - t, lines


def detect_nightly(conn, detect_dates):
    """Step 5: the detectors once a night per sensor (refresh_schedule.detectors_due)."""
    if not DETECTORS:
        return
    sensors = conn.execute(
        "SELECT s.id, s.name, a.tz FROM sensors s JOIN accounts a ON a.id = s.account_id "
        "JOIN sensor_summary ss ON ss.sensor_id = s.id ORDER BY s.id", prepare=False).fetchall()
    for s in sensors:
        due, day = rs.detectors_due(rs.utc_now(), s["tz"], detect_dates.get(s["id"]))
        if not due:
            continue
        detect_dates[s["id"]] = day
        to = rs.utc_now()
        frm = to - timedelta(hours=DETECT_WINDOW_H)
        for name in DETECTORS:
            if _stop:
                return
            ok, dur, lines = run_detector(name, s["name"], frm, to)
            log.info("detector %s sensor=%s window=%.0fh ok=%s s=%.1f %s", name, s["name"], DETECT_WINDOW_H,
                     ok, dur, " | ".join(lines))


def _on_signal(signum, frame):
    global _stop
    _stop = True


def connect():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row,
                           application_name="wifi-audit-refresher", connect_timeout=10)
    conn.execute("SET jit = off")
    return conn


def round_once(conn, baseline_dates):
    """One round; returns the duration of steps 1-2 in seconds (the nightly
    baselines are not paced by the duty cycle)."""
    t0 = time.monotonic()
    close_expired(conn)
    restore_taken(conn)
    dirty = conn.execute(
        "SELECT i.sensor_id, s.name, i.dirty_from, i.dirty_at, i.dirty_since "
        "FROM ingest_state i JOIN sensors s ON s.id = i.sensor_id "
        "WHERE i.dirty_from IS NOT NULL ORDER BY i.sensor_id", prepare=False).fetchall()
    for d in dirty:
        t, started = time.monotonic(), rs.utc_now()
        taken = conn.execute(SQL_TAKE_MARK, (d["sensor_id"],), prepare=False).fetchone()
        if taken is None:
            continue
        try:
            r = conn.execute("SELECT * FROM refresh_rollups(%s, %s)", (d["sensor_id"], taken["dirty_from"]),
                             prepare=False).fetchone()
        except psycopg.Error as e:
            record(conn, "rollups", started, time.monotonic() - t, d["sensor_id"], ok=False,
                   refresh_from=taken["dirty_from"], error=str(e)[:500])
            if not conn.closed:
                try:
                    restore_taken(conn)
                except psycopg.Error:
                    pass        # stays in taken_from for the next round
            raise
        # only now is the window done; if this statement is lost the next round refreshes it once more
        conn.execute(SQL_MARK_DONE, (d["sensor_id"],), prepare=False)
        dur = time.monotonic() - t
        waited = (rs.utc_now() - taken["dirty_since"]).total_seconds() if taken["dirty_since"] else 0
        log.info("refresh_rollups sensor=%s from=%s s=%.2f ap_hours=%s sensor_hours=%s waited_s=%.0f",
                 d["name"], taken["dirty_from"].isoformat(), dur, r["ap_hours"], r["sensor_hours"], waited)
        record(conn, "rollups", started, dur, d["sensor_id"], refresh_from=taken["dirty_from"],
               waited_s=round(waited, 1), ap_hours=r["ap_hours"], sensor_hours=r["sensor_hours"])
    t, started = time.monotonic(), rs.utc_now()
    contact = conn.execute("SELECT st.sensor_id, refresh_contact_periods(st.sensor_id) AS gaps "
                           "FROM sensor_status st ORDER BY st.sensor_id", prepare=False).fetchall()
    if contact:
        dur, gaps = time.monotonic() - t, sum(c["gaps"] for c in contact)
        log.info("refresh_contact_periods sensors=%d gaps=%d s=%.2f", len(contact), gaps, dur)
        record(conn, "contact_periods", started, dur, gaps=gaps)
    _notifier.check(conn)
    took = time.monotonic() - t0
    if BASELINES:
        # the last run's local day from the table itself (computed_at), so a
        # restart of the container does not repeat a ~4-min refresh
        sensors = conn.execute(
            "SELECT s.id, s.name, a.tz, "
            "       (SELECT max(b.computed_at) FROM ap_baselines b WHERE b.sensor_id = s.id) AS last_run "
            "FROM sensors s JOIN accounts a ON a.id = s.account_id "
            "JOIN sensor_summary ss ON ss.sensor_id = s.id ORDER BY s.id", prepare=False).fetchall()
        for s in sensors:
            last = baseline_dates.get(s["id"]) or rs.local_day(s["last_run"], s["tz"])
            due, day = rs.baseline_due(rs.utc_now(), s["tz"], last)
            if not due:
                continue
            t, started = time.monotonic(), rs.utc_now()
            n = conn.execute("SELECT refresh_ap_baselines(%s, 200, 2) AS n", (s["id"],), prepare=False).fetchone()["n"]
            baseline_dates[s["id"]] = day
            dur = time.monotonic() - t
            log.info("refresh_ap_baselines sensor=%s day=%s baselines=%s s=%.2f", s["name"], day, n, dur)
            record(conn, "ap_baselines", started, dur, s["id"], baselines=n)
    return took


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    global _notifier
    try:
        cfg = notify.config()
    except ValueError as e:
        log.warning("%s - notifications off", e)
        cfg = None
    _notifier = notify.Notifier(cfg)
    log.info("refresher start: interval=%ss duty factor=%s baselines=%s detectors=%s notify=%s", INTERVAL_S,
             rs.DUTY_FACTOR, BASELINES, ",".join(DETECTORS) or "off",
             "off" if cfg is None else "on (%d recipient(s))" % len(cfg["to"]))
    conn, baseline_dates, detect_dates = None, {}, {}
    while not _stop:
        try:
            if conn is None or conn.closed:
                conn = connect()
            started = rs.utc_now()
            took = round_once(conn, baseline_dates)
            detect_nightly(conn, detect_dates)
            delay = rs.next_delay(INTERVAL_S, took)
            if took > INTERVAL_S / rs.DUTY_FACTOR:
                log.info("round took %.1f s: next in %.0f s (duty-cycle cap)", took, delay)
            record(conn, "round", started, took, next_in_s=round(delay, 1))
        except psycopg.Error as e:
            log.warning("database error: %s - retrying in %d s", e, RETRY_S)
            try:
                if conn is not None:
                    conn.close()
            except psycopg.Error:
                pass
            conn, delay = None, RETRY_S
        end = time.monotonic() + delay
        while not _stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
    if conn is not None:
        conn.close()
    log.info("refresher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
