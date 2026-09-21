"""Tests for the detections sink: the evidence bookkeeping across runs and the
SQL contract (what a refresh must never touch). No database."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from detection import detections  # noqa: E402


class RefreshTest(unittest.TestCase):
    def test_first_run(self):
        now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
        ev = detections.refreshed_evidence({"a": 1}, None, now)
        self.assertEqual(ev, {"a": 1, "runs": 1, "first_run_at": "2026-09-21T10:00:00.000Z",
                              "last_run_at": "2026-09-21T10:00:00.000Z"})

    def test_later_run_keeps_first_run_and_counts(self):
        now = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
        old = {"a": 1, "runs": 2, "first_run_at": "2026-09-21T10:00:00.000Z", "stale": True}
        ev = detections.refreshed_evidence({"a": 2}, old, now)
        self.assertEqual(ev["runs"], 3)
        self.assertEqual(ev["first_run_at"], "2026-09-21T10:00:00.000Z")
        self.assertEqual(ev["last_run_at"], "2026-09-22T10:00:00.000Z")
        self.assertEqual(ev["a"], 2)
        self.assertNotIn("stale", ev)          # latest run wins

    def test_update_never_touches_the_acknowledgement(self):
        sql = detections._UPDATE_SQL.lower()
        for col in ("acked", "acked_at", "ack_note", "created_at"):
            self.assertNotIn(col, sql)
        self.assertIn("where id = %(id)s", sql)

    def test_overlap_lookup_is_scoped_and_locked(self):
        sql = detections._OVERLAP_SQL.lower()
        self.assertIn("type = %(type)s", sql)
        self.assertIn("coalesce(device_key, upper(mac::text)) = %(subject)s", sql)
        self.assertIn("for update", sql)

    def test_subject(self):
        f = detections.Finding(type="t", ts=None, severity="low", device_key=None, mac="ec:58:ea:00:00:01",
                               ssid=None, summary="", evidence={}, window_start=None, window_end=None)
        self.assertEqual(f.subject, "EC:58:EA:00:00:01")
        f.device_key = "KEY"
        self.assertEqual(f.subject, "KEY")


if __name__ == "__main__":
    unittest.main()
