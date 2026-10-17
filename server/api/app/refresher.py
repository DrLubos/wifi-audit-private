"""The refresher (docs/live-upload.md 6.4): `python -m app.refresher`, the
compose service `refresher` (the api image, its own container - a slow
refresh never holds one of the api's pool connections).

Live ingest marks a sensor dirty (ingest_state.dirty_from = the earliest ts
it inserted; ingest_merge()) instead of refreshing per request. Each round:

1. per dirty sensor refresh_rollups(sensor, dirty_from), then clear the mark
   unless an ingest moved it meanwhile (dirty_at changed) - the next round
   refreshes again from the earlier of the two;
2. refresh_contact_periods() for every sensor that ever uploaded
   ('unreachable' annotations from heartbeat gaps);
3. once a night at 03:30 in the account's time zone, refresh_ap_baselines().

The next round starts no earlier than max(REFRESH_INTERVAL_S, 4 x the last
round's duration) after this one started (refresh_schedule.next_delay(): at
most a fifth of the e2-micro's core, however slow a refresh gets). Every
round logs one line per step with its duration - the measurement the
interval is chosen from (N8). Statements run in autocommit: each function is
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


def _on_signal(signum, frame):
    global _stop
    _stop = True


def connect():
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row,
                           application_name="wifi-audit-refresher", connect_timeout=10)
    conn.execute("SET jit = off")
    return conn


def round_once(conn, baseline_dates):
    """One round; returns its duration in seconds."""
    t0 = time.monotonic()
    dirty = conn.execute(
        "SELECT i.sensor_id, s.name, i.dirty_from, i.dirty_at, i.dirty_since "
        "FROM ingest_state i JOIN sensors s ON s.id = i.sensor_id "
        "WHERE i.dirty_from IS NOT NULL ORDER BY i.sensor_id", prepare=False).fetchall()
    for d in dirty:
        t = time.monotonic()
        r = conn.execute("SELECT * FROM refresh_rollups(%s, %s)", (d["sensor_id"], d["dirty_from"]),
                         prepare=False).fetchone()
        cleared = conn.execute(
            "UPDATE ingest_state SET dirty_from = NULL, dirty_since = NULL "
            "WHERE sensor_id = %s AND dirty_at = %s RETURNING 1", (d["sensor_id"], d["dirty_at"]),
            prepare=False).fetchone()
        log.info("refresh_rollups sensor=%s from=%s s=%.2f ap_hours=%s sensor_hours=%s waited_s=%.0f%s",
                 d["name"], d["dirty_from"].isoformat(), time.monotonic() - t, r["ap_hours"], r["sensor_hours"],
                 (rs.utc_now() - d["dirty_since"]).total_seconds() if d["dirty_since"] else 0,
                 "" if cleared else " (new data meanwhile: again next round)")
    t = time.monotonic()
    contact = conn.execute("SELECT st.sensor_id, refresh_contact_periods(st.sensor_id) AS gaps "
                           "FROM sensor_status st ORDER BY st.sensor_id", prepare=False).fetchall()
    if contact:
        log.info("refresh_contact_periods sensors=%d gaps=%d s=%.2f", len(contact),
                 sum(c["gaps"] for c in contact), time.monotonic() - t)
    if BASELINES:
        sensors = conn.execute(
            "SELECT s.id, s.name, a.tz FROM sensors s JOIN accounts a ON a.id = s.account_id "
            "JOIN sensor_summary ss ON ss.sensor_id = s.id ORDER BY s.id", prepare=False).fetchall()
        for s in sensors:
            due, day = rs.baseline_due(rs.utc_now(), s["tz"], baseline_dates.get(s["id"]))
            if not due:
                continue
            t = time.monotonic()
            n = conn.execute("SELECT refresh_ap_baselines(%s, 200, 2) AS n", (s["id"],), prepare=False).fetchone()["n"]
            baseline_dates[s["id"]] = day
            log.info("refresh_ap_baselines sensor=%s day=%s baselines=%s s=%.2f", s["name"], day, n,
                     time.monotonic() - t)
    return time.monotonic() - t0


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
            took = round_once(conn, baseline_dates)
            delay = rs.next_delay(INTERVAL_S, took)
            if took > INTERVAL_S / rs.DUTY_FACTOR:
                log.info("round took %.1f s: next in %.0f s (duty-cycle cap)", took, delay)
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
