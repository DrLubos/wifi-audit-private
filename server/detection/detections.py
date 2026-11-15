"""Sink for detector findings: the only code that writes the `detections` table.

One row per (sensor, detector type, subject, episode). A finding is matched to an
existing row of the same sensor/type/subject whose stored window
(evidence.window_start/window_end) overlaps the new one, widened by the
detector's episode gap; a match is refreshed in place (ts, severity, summary,
evidence, identity), otherwise a row is inserted. So a re-run over the same,
a shifted or an overlapping window never duplicates an episode, and re-running
with other thresholds re-grades the stored episode (latest run wins). What is
never touched on a refresh: id, status, status_at, status_note, created_at -
the operator's label survives (schema 9; acked / ack_note before). A refresh
sets updated_at (the dashboard's ETags). The evidence keeps `runs`, `first_run_at`
and `last_run_at` across refreshes.

Two findings of one subject are the same episode only when they also carry the
same `signal` (evidence.signal; '' when a detector has one kind of finding):
evil_twin's membership finding (a) spans the whole scan and would otherwise
swallow the RSSI episodes (b) of the same AP. A finding marked `extend` (a
standing condition such as (a), not an episode rebuilt from its events) keeps
the earliest start and the union of the windows on a refresh.

A run that sees only part of a stored episode would refresh it with that part
(a later window that starts inside a flood stored its tail as the whole
episode, with a later ts): widen() gives the detector the scan range that
covers every stored episode its window touches, so the episode is rebuilt
whole from the source rows.

The subject is the AP's device_key, or its BSSID (upper-case text) when no
device row exists; schema.sql enforces uniqueness of (sensor_id, type, subject,
ts) with the detections_dedupe index as the last line of defence.
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .common import UTC, iso

_OVERLAP_SQL = """
SELECT id, ts, severity, status, evidence
FROM detections
WHERE sensor_id = %(sid)s AND kind = 'detection' AND type = %(type)s
  AND coalesce(device_key, upper(mac::text)) = %(subject)s
  AND evidence ? 'window_start' AND evidence ? 'window_end'
  AND coalesce(evidence->>'signal', '') = %(signal)s
  AND (evidence->>'window_end')::timestamptz   >= %(start)s - %(gap_s)s * interval '1 second'
  AND (evidence->>'window_start')::timestamptz <= %(end)s   + %(gap_s)s * interval '1 second'
ORDER BY ts
FOR UPDATE"""

# The span of the stored episodes (of one type and signal) that a scan range
# touches: what widen() adds to the range. A SELECT - it runs in the detector's
# READ ONLY analysis transaction.
_SPAN_SQL = """
SELECT min((evidence->>'window_start')::timestamptz) AS lo, max((evidence->>'window_end')::timestamptz) AS hi
FROM detections
WHERE sensor_id = %(sid)s AND kind = 'detection' AND type = %(type)s
  AND evidence ? 'window_start' AND evidence ? 'window_end'
  AND coalesce(evidence->>'signal', '') = %(signal)s
  AND (evidence->>'window_end')::timestamptz   >= %(start)s
  AND (evidence->>'window_start')::timestamptz <= %(end)s"""

_INSERT_SQL = """
INSERT INTO detections (sensor_id, ts, type, severity, device_key, mac, ssid, summary, evidence)
VALUES (%(sid)s, %(ts)s, %(type)s, %(severity)s, %(device_key)s, %(mac)s::macaddr, %(ssid)s,
        %(summary)s, %(evidence)s::jsonb)
RETURNING id"""

# status / status_at / status_note / created_at are deliberately absent.
_UPDATE_SQL = """
UPDATE detections
SET ts = %(ts)s, severity = %(severity)s, device_key = %(device_key)s, mac = %(mac)s::macaddr,
    ssid = %(ssid)s, summary = %(summary)s, evidence = %(evidence)s::jsonb, updated_at = now()
WHERE id = %(id)s"""


@dataclass
class Finding:
    """What a detector hands to write(): one detections row plus its episode window."""
    type: str
    ts: datetime
    severity: str
    device_key: str | None
    mac: str | None                 # BSSID as text (any case); NULL only if unknown
    ssid: str | None
    summary: str
    evidence: dict
    window_start: datetime
    window_end: datetime
    action: str | None = None       # filled by write(): 'insert' | 'update'
    id: int | None = None
    extra: dict = field(default_factory=dict)   # detector-private, not stored
    signal: str = ""                # which of a detector's kinds of finding (evidence.signal)
    extend: bool = False            # a standing condition: a refresh keeps the earliest start

    @property
    def subject(self):
        return self.device_key or (self.mac.upper() if self.mac else None)


def refreshed_evidence(new_evidence, old_evidence, now=None):
    """Evidence to store: the new run's, carrying the run bookkeeping forward."""
    now_iso = iso(now or datetime.now(UTC))
    ev = dict(new_evidence)
    old = old_evidence or {}
    ev["runs"] = int(old.get("runs") or 0) + 1
    ev["first_run_at"] = old.get("first_run_at") or now_iso
    ev["last_run_at"] = now_iso
    return ev


def find_existing(conn, sid, finding, gap_s):
    """Stored rows of the same sensor/type/subject/signal whose window overlaps the finding's."""
    if finding.subject is None:
        return []
    return conn.execute(_OVERLAP_SQL, {
        "sid": sid, "type": finding.type, "subject": finding.subject, "signal": finding.signal,
        "start": finding.window_start, "end": finding.window_end, "gap_s": gap_s,
    }).fetchall()


def widen(conn, sid, type_, scan_from, scan_to, gap_s, signal=""):
    """The scan range a detector must read so that every stored episode its
    range touches is rebuilt whole: (scan_from, scan_to) stretched over the
    stored windows, each by the episode gap. Unchanged when nothing is stored
    there. One step, not a fixed point: an episode that was never stored and
    began before scan_from still starts at the range's edge."""
    row = conn.execute(_SPAN_SQL, {"sid": sid, "type": type_, "signal": signal,
                                   "start": scan_from, "end": scan_to}).fetchone()
    if not row or row["lo"] is None:
        return scan_from, scan_to
    gap = timedelta(seconds=gap_s)
    return min(scan_from, row["lo"] - gap), max(scan_to, row["hi"] + gap)


def _when(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def extended(finding, old):
    """(ts, evidence) of an `extend` finding refreshed over the stored row
    `old`: the earliest start and the union of the two windows."""
    ev = dict(finding.evidence)
    ts = finding.ts
    stored = old["evidence"] or {}
    if stored.get("window_start") and _when(stored["window_start"]) < finding.window_start:
        ev["window_start"] = stored["window_start"]
        ts = min(ts, old["ts"])
    if stored.get("window_end") and _when(stored["window_end"]) > finding.window_end:
        ev["window_end"] = stored["window_end"]
    return ts, ev


def write(conn, sid, finding, gap_s, now=None):
    """Insert the finding or refresh the stored row it overlaps. Sets
    finding.action / finding.id and returns the action. Must run inside the
    caller's (read-write) transaction."""
    existing = find_existing(conn, sid, finding, gap_s)
    if len(existing) > 1:
        print("  warning: %d stored %s rows overlap the episode of %s at %s; refreshing id %d"
              % (len(existing), finding.type, finding.subject, iso(finding.window_start),
                 existing[0]["id"]), file=sys.stderr)
    old = existing[0] if existing else None
    ts, new_evidence = extended(finding, old) if old and finding.extend else (finding.ts, finding.evidence)
    if finding.signal:                              # the match key of the next run
        new_evidence = dict(new_evidence, signal=finding.signal)
    evidence = refreshed_evidence(new_evidence, old["evidence"] if old else None, now)
    params = {
        "sid": sid, "ts": ts, "type": finding.type, "severity": finding.severity,
        "device_key": finding.device_key, "mac": finding.mac, "ssid": finding.ssid,
        "summary": finding.summary, "evidence": json.dumps(evidence, default=str),
    }
    if old is None:
        finding.id = conn.execute(_INSERT_SQL, params).fetchone()["id"]
        finding.action = "insert"
    else:
        params["id"] = old["id"]
        conn.execute(_UPDATE_SQL, params)
        finding.id = old["id"]
        finding.action = "update"
    finding.evidence = evidence
    return finding.action
