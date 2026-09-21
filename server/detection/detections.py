"""Sink for detector findings: the only code that writes the `detections` table.

One row per (sensor, detector type, subject, episode). A finding is matched to an
existing row of the same sensor/type/subject whose stored window
(evidence.window_start/window_end) overlaps the new one, widened by the
detector's episode gap; a match is refreshed in place (ts, severity, summary,
evidence, identity), otherwise a row is inserted. So a re-run over the same,
a shifted or an overlapping window never duplicates an episode, and re-running
with other thresholds re-grades the stored episode (latest run wins). What is
never touched on a refresh: id, acked, acked_at, ack_note, created_at - the
operator's acknowledgement survives. The evidence keeps `runs`, `first_run_at`
and `last_run_at` across refreshes.

The subject is the AP's device_key, or its BSSID (upper-case text) when no
device row exists; schema.sql enforces uniqueness of (sensor_id, type, subject,
ts) with the detections_dedupe index as the last line of defence.
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime

from .common import UTC, iso

_OVERLAP_SQL = """
SELECT id, ts, severity, acked, evidence
FROM detections
WHERE sensor_id = %(sid)s AND type = %(type)s
  AND coalesce(device_key, upper(mac::text)) = %(subject)s
  AND evidence ? 'window_start' AND evidence ? 'window_end'
  AND (evidence->>'window_end')::timestamptz   >= %(start)s - %(gap_s)s * interval '1 second'
  AND (evidence->>'window_start')::timestamptz <= %(end)s   + %(gap_s)s * interval '1 second'
ORDER BY ts
FOR UPDATE"""

_INSERT_SQL = """
INSERT INTO detections (sensor_id, ts, type, severity, device_key, mac, ssid, summary, evidence)
VALUES (%(sid)s, %(ts)s, %(type)s, %(severity)s, %(device_key)s, %(mac)s::macaddr, %(ssid)s,
        %(summary)s, %(evidence)s::jsonb)
RETURNING id"""

# acked / acked_at / ack_note / created_at are deliberately absent.
_UPDATE_SQL = """
UPDATE detections
SET ts = %(ts)s, severity = %(severity)s, device_key = %(device_key)s, mac = %(mac)s::macaddr,
    ssid = %(ssid)s, summary = %(summary)s, evidence = %(evidence)s::jsonb
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
    """Stored rows of the same sensor/type/subject whose window overlaps the finding's."""
    if finding.subject is None:
        return []
    return conn.execute(_OVERLAP_SQL, {
        "sid": sid, "type": finding.type, "subject": finding.subject,
        "start": finding.window_start, "end": finding.window_end, "gap_s": gap_s,
    }).fetchall()


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
    evidence = refreshed_evidence(finding.evidence, old["evidence"] if old else None, now)
    params = {
        "sid": sid, "ts": finding.ts, "type": finding.type, "severity": finding.severity,
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
