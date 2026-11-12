"""Discard mode, the ingest side (docs/discard-mode.md): what a batch that
arrives while its sensor's window is on records instead of being merged, and
the 200 response that moves the uploader's cursor on. Standard library only
(tests/test_discard.py); routes.py runs the SQL (sql/ingest.sql).

While a window is on, /batch authenticates, rate-limits and validates as
usual (a 422 is still a 422), then stores only the heartbeat (history,
status, contact, adapter, the configuration report) and one
sensor_discarded_batches row: the discarded polls' count, their buffer row
ids and times - the record the loss rule (discarded_seqs()) and the excluded
window (discard_intervals()) read. Gaps the batch reports: 'unverified' polls
(a boot without its clock anchor - polls of the window the uploader could not
place in time) are part of the window; 'lost' ones (pruned on the sensor
before the upload, i.e. at least the retention old) happened before it and are
stored as loss as usual.
"""

import time

from ..v2.periods import iso
from . import validate


def summary(b):
    """{polls, seq_from, seq_to, ts_from, ts_to, rows, unverified_polls} of a
    validated batch (validate.batch): ts_* in unix seconds (None without a
    poll), the seq range over the polls and the unverified gaps."""
    cols = validate.columns("polls")
    seq_i, ts_i = cols.index("seq"), cols.index("ts")
    polls = b["tables"].get("polls") or []
    seqs = [p[seq_i] for p in polls]
    unverified = [g for g in b["gaps"] if g["reason"] == "unverified"]
    for g in unverified:
        seqs += [g["seq_from"], g["seq_to"]]
    times = [p[ts_i] for p in polls]
    return {"polls": len(polls), "seq_from": min(seqs, default=None), "seq_to": max(seqs, default=None),
            "ts_from": min(times, default=None), "ts_to": max(times, default=None),
            "rows": {name: len(rows) for name, rows in b["tables"].items() if rows},
            "unverified_polls": sum(g["polls"] for g in unverified)}


def stored_gaps(b):
    """The gaps a discarded batch still stores (sensor_seq_gaps): the lost ones."""
    return [g for g in b["gaps"] if g["reason"] == "lost"]


def response(b, replay, ms, delivered, until):
    """The 200 of a discarded batch: the same keys as a merged one (nothing
    staged or inserted) and "discarded" - the window's end and the polls."""
    return {"batch_seq": b["batch_seq"], "replay": bool(replay), "staged": {}, "inserted": {}, "conflicts": {},
            "ms": ms, "server_ts": time.time(), "config": delivered,
            "discarded": {"until": iso(until), "polls": len(b["tables"].get("polls") or [])}}
