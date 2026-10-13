"""The RQ2 label export: every event with its current outcome (labels.csv)
and every change of a label (label_history.csv) as CSV - the false-positive
dataset of the thesis. Standard library only (tests/test_export.py).

Columns are fixed and carry no evidence or summary (they can name client
MACs); the BSSID is blank unless it is an account AP (the query decides); the
free text is the operator's own. A cell that starts like a spreadsheet formula
gets a leading apostrophe, so opening the file never runs anything.
"""

import csv
import io
from datetime import datetime, timezone

LABEL_COLUMNS = ("event_id", "ts_utc", "ts_end_utc", "sensor", "kind", "type", "type_label", "severity", "bssid",
                 "ssid", "outcome", "reason", "note", "labelled_at_utc")
HISTORY_COLUMNS = ("event_id", "at_utc", "kind", "type", "old_outcome", "old_reason", "old_note",
                   "new_outcome", "new_reason", "new_note")
FORMULA = ("=", "+", "-", "@", "\t", "\r")


def ts(v):
    """UTC ISO 8601 with a Z, seconds precision; blank for None."""
    if v is None:
        return ""
    return v.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_cell(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return ts(v)
    s = str(v)
    return "'" + s if s.startswith(FORMULA) else s


def to_csv(columns, rows):
    out = io.StringIO(newline="")
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([safe_cell(c) for c in r])
    return out.getvalue()


def labels_csv(rows):
    """rows: events.export_labels (one per event, oldest first)."""
    return to_csv(LABEL_COLUMNS, [
        (r["id"], ts(r["ts"]), ts(r["ts_end"]), r["sensor"], r["kind"], r["type"], r["type_label"], r["severity"],
         r["bssid"], r["ssid"], r["status"], r["status_reason"], r["status_note"], ts(r["status_at"])) for r in rows])


def history_csv(rows):
    """rows: events.export_history (one per change of a label, oldest first)."""
    return to_csv(HISTORY_COLUMNS, [
        (r["event_id"], ts(r["at"]), r["kind"], r["type"], r["old_status"], r["old_reason"], r["old_note"],
         r["new_status"], r["new_reason"], r["new_note"]) for r in rows])


def filename(name, now):
    return "wifi-audit-%s-%s.csv" % (name, now.astimezone(timezone.utc).strftime("%Y%m%d"))
