"""The refresher (docs/live-upload.md 6.4): `python -m app.refresher`, the
compose service `refresher` (the api image, its own container - a slow
refresh never holds one of the api's pool connections).

Live ingest marks a sensor dirty (ingest_state.dirty_from = the earliest ts
it inserted; ingest_merge()) instead of refreshing per request. Each round:

1. per dirty sensor: take the mark and clear it in one statement, then
   refresh_rollups(sensor, dirty_from); an ingest during the refresh sets a new
   mark for the next round (only its own new rows), and a failed refresh puts
   the taken mark back - kept in memory when the connection itself died
   (Postgres restarting after a crash) and written back first thing in the
   next round, so a crash never loses the window it was refreshing. (Clearing only "if no ingest moved it meanwhile" never
   cleared it with a batch every 30 s and rounds longer than that: every
   round re-refreshed days of hours - found on the first live day.)
2. refresh_contact_periods() for every sensor that ever uploaded
   ('unreachable' annotations from heartbeat gaps);
3. once a night at 03:30 in the account's time zone, refresh_ap_baselines().

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
import sys
import time

import psycopg
from psycopg.rows import dict_row

from . import refresh_schedule as rs

log = logging.getLogger("refresher")

INTERVAL_S = float(os.environ.get("REFRESH_INTERVAL_S", "60"))
BASELINES = os.environ.get("REFRESH_BASELINES", "1").strip() not in ("0", "false", "no", "off")
RETRY_S = 30

_stop = False
_carry = {}     # sensor_id -> (dirty_from, dirty_since) taken by a refresh that failed
_recording = True

_RUN_COLS = ("refresh_from", "waited_s", "ap_hours", "sensor_hours", "gaps", "baselines", "next_in_s", "error")


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


def _restore_marks(conn):
    """Put carried marks back (earliest wins); a mark leaves _carry only once written."""
    for sid, (dfrom, dsince) in list(_carry.items()):
        conn.execute(
            "INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at) "
            "VALUES (%s, %s, %s, now()) ON CONFLICT (sensor_id) DO UPDATE SET "
            "dirty_from = least(i.dirty_from, EXCLUDED.dirty_from), "
            "dirty_since = coalesce(least(i.dirty_since, EXCLUDED.dirty_since), EXCLUDED.dirty_since)",
            (sid, dfrom, dsince), prepare=False)
        del _carry[sid]


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
    if _carry:
        log.info("restoring %d dirty mark(s) of a failed refresh", len(_carry))
        _restore_marks(conn)
    dirty = conn.execute(
        "SELECT i.sensor_id, s.name, i.dirty_from, i.dirty_at, i.dirty_since "
        "FROM ingest_state i JOIN sensors s ON s.id = i.sensor_id "
        "WHERE i.dirty_from IS NOT NULL ORDER BY i.sensor_id", prepare=False).fetchall()
    for d in dirty:
        t, started = time.monotonic(), rs.utc_now()
        taken = conn.execute(
            "UPDATE ingest_state i SET dirty_from = NULL, dirty_since = NULL "
            "FROM (SELECT sensor_id, dirty_from, dirty_since FROM ingest_state "
            "      WHERE sensor_id = %s AND dirty_from IS NOT NULL FOR UPDATE) o "
            "WHERE i.sensor_id = o.sensor_id RETURNING o.dirty_from, o.dirty_since",
            (d["sensor_id"],), prepare=False).fetchone()
        if taken is None:
            continue
        try:
            r = conn.execute("SELECT * FROM refresh_rollups(%s, %s)", (d["sensor_id"], taken["dirty_from"]),
                             prepare=False).fetchone()
        except psycopg.Error as e:
            record(conn, "rollups", started, time.monotonic() - t, d["sensor_id"], ok=False,
                   refresh_from=taken["dirty_from"], error=str(e)[:500])
            old = _carry.get(d["sensor_id"])
            dfrom, dsince = taken["dirty_from"], taken["dirty_since"]
            if old is not None:
                dfrom = min(dfrom, old[0])
                dsince = min(x for x in (dsince, old[1]) if x is not None) if (dsince or old[1]) else None
            _carry[d["sensor_id"]] = (dfrom, dsince)
            if not conn.closed:
                try:
                    _restore_marks(conn)
                except psycopg.Error:
                    pass        # stays in _carry for the next round
            raise
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
    log.info("refresher start: interval=%ss duty factor=%s baselines=%s", INTERVAL_S, rs.DUTY_FACTOR, BASELINES)
    conn, baseline_dates = None, {}
    while not _stop:
        try:
            if conn is None or conn.closed:
                conn = connect()
            started = rs.utc_now()
            took = round_once(conn, baseline_dates)
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
