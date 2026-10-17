"""Ingest logic without a database (docs/live-upload.md 6, 8.4, 9.2):
rate limits (app/ingest/ratelimit.py), stall events (stalls.py), the
heartbeat helpers (heartbeat.py) and the refresher's schedule
(app/refresh_schedule.py). Standard library only."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import refresh_schedule as rs  # noqa: E402
from app.ingest import heartbeat as hb  # noqa: E402
from app.ingest import ratelimit, stalls  # noqa: E402

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def at(seconds):
    return datetime.fromtimestamp(NOW.timestamp() + seconds, timezone.utc)


class Buckets(unittest.TestCase):
    def test_steady_rate_and_burst(self):
        b = ratelimit.Buckets(per_minute=120, burst=10)
        ok = [b.allow("s1", 0.0)[0] for _ in range(10)]
        self.assertEqual(ok, [True] * 10)
        allowed, retry = b.allow("s1", 0.0)
        self.assertFalse(allowed)
        self.assertGreater(retry, 0)
        self.assertTrue(b.allow("s1", 0.5)[0])        # 2 tokens a second refill
        self.assertTrue(b.allow("s2", 0.0)[0])        # per key

    def test_one_batch_per_poll_is_far_below(self):
        b = ratelimit.Buckets(per_minute=120, burst=20)
        self.assertTrue(all(b.allow("s", t * 2.0)[0] for t in range(300)))   # catch-up: one every 2 s


class FailWindow(unittest.TestCase):
    def test_block_after_too_many_failures(self):
        f = ratelimit.FailWindow(limit=3, window_s=60, block_s=600)
        for t in (0, 1, 2):
            self.assertEqual(f.blocked("1.2.3.4", t), 0)
            f.failed("1.2.3.4", t)
        self.assertGreater(f.blocked("1.2.3.4", 3), 0)
        self.assertEqual(f.blocked("5.6.7.8", 3), 0)
        self.assertEqual(f.blocked("1.2.3.4", 2 + 601), 0)

    def test_old_failures_age_out(self):
        f = ratelimit.FailWindow(limit=3, window_s=60, block_s=600)
        for t in (0, 30, 100):
            f.failed("ip", t)
        self.assertEqual(f.blocked("ip", 101), 0)


class Stalls(unittest.TestCase):
    def test_reject_opens_after_three_over_ten_minutes(self):
        st = stalls.clear()
        st, open_ = stalls.after_reject(st, 7, "polls row 1: ok is required", NOW)
        self.assertFalse(open_)
        st, open_ = stalls.after_reject(st, 7, "polls row 1: ok is required", at(300))
        self.assertFalse(open_)                        # 2 refusals
        st, open_ = stalls.after_reject(st, 7, "polls row 1: ok is required", at(400))
        self.assertFalse(open_)                        # 3, but only 400 s
        st, open_ = stalls.after_reject(st, 7, "polls row 1: ok is required", at(601))
        self.assertTrue(open_)
        self.assertEqual((st["reject_seq"], st["reject_count"], st["reject_since"]), (7, 4, NOW))

    def test_another_batch_restarts_the_count(self):
        st, _ = stalls.after_reject(stalls.clear(), 7, "x", NOW)
        st, _ = stalls.after_reject(st, 8, "y", at(700))
        self.assertEqual((st["reject_seq"], st["reject_count"], st["reject_since"]), (8, 1, at(700)))

    def test_accept_clears(self):
        st, _ = stalls.after_reject(stalls.clear(), 7, "x", NOW)
        st = stalls.after_accept(st)
        self.assertEqual((st["reject_seq"], st["reject_count"], st["reject_since"]), (None, 0, None))
        self.assertEqual(stalls.CLOSE_ON_ACCEPT, ("upload_rejected", "upload_unauthorized"))

    def test_auth_failures(self):
        st = stalls.clear()
        for t, want in ((0, False), (200, False), (650, True)):
            st, open_ = stalls.after_auth_fail(st, at(t))
            self.assertEqual(open_, want, t)
        st = stalls.after_auth_ok(st)
        self.assertEqual((st["auth_fail_since"], st["auth_fail_count"]), (None, 0))

    def test_event_texts_name_no_secret(self):
        summary, evidence = stalls.event("upload_rejected", {"reject_seq": 7, "reject_count": 4,
                                                             "reject_since": NOW, "reject_reason": "bad"})
        self.assertIn("refuses", summary)
        self.assertEqual(evidence["batch_seq"], 7)
        summary, evidence = stalls.event("upload_retention_risk", {}, oldest=NOW, retention_days=14)
        self.assertIn("retention", summary)


class Heartbeat(unittest.TestCase):
    def test_clock_offset(self):
        # offset + one-way latency; the previous request's RTT is not subtracted
        self.assertAlmostEqual(hb.clock_offset_s(1000.0, 990.0, 2000), 10.0)
        self.assertAlmostEqual(hb.clock_offset_s(1000.0, 999.8, None), 0.2)

    def test_history_at_most_once_a_minute(self):
        self.assertTrue(hb.history_due(None, NOW))
        self.assertFalse(hb.history_due(at(-30), NOW))
        self.assertTrue(hb.history_due(at(-55), NOW))

    def test_adapter_actions(self):
        seeded = {"usb_id": None, "mac": None}
        rep = {"usb_id": "0bda:c811", "mac": "00:11:22:33:44:55", "driver": "rtw88_8821cu",
               "chipset": "RTL8821CU", "interface": "wlan1"}
        self.assertEqual(hb.adapter_action(seeded, rep), "fill")          # D8: no boundary
        self.assertEqual(hb.adapter_action(None, rep), "fill")
        self.assertEqual(hb.adapter_action(rep, rep), "none")
        self.assertEqual(hb.adapter_action({"usb_id": "0bda:c811", "mac": None}, rep), "fill")
        self.assertEqual(hb.adapter_action(rep, dict(rep, mac="00:11:22:33:44:66")), "change")
        self.assertEqual(hb.adapter_action(rep, dict(rep, usb_id="2357:0120")), "change")
        self.assertEqual(hb.adapter_action(rep, None), "none")            # adapter absent: not a change
        self.assertEqual(hb.adapter_action(rep, {"usb_id": None, "mac": None}), "none")
        self.assertEqual(hb.adapter_action(rep, dict(rep, interface="wlan2")), "none")   # display only

    def test_retention_risk(self):
        day = 86400
        base = {"retention_days": 14, "backlog_polls": 10}
        self.assertFalse(hb.retention_risk(dict(base, backlog_oldest_ts=NOW.timestamp() - 11 * day), NOW))
        self.assertTrue(hb.retention_risk(dict(base, backlog_oldest_ts=NOW.timestamp() - 12.5 * day), NOW))
        self.assertFalse(hb.retention_risk(dict(base, backlog_oldest_ts=NOW.timestamp() - 13 * day,
                                                backlog_polls=0), NOW))
        self.assertFalse(hb.retention_risk({}, NOW))


class Schedule(unittest.TestCase):
    def test_duty_cycle(self):
        self.assertEqual(rs.next_delay(60, 5), 55)            # interval wins
        self.assertEqual(rs.next_delay(60, 30), 90)           # 4 x 30 s after the start = 90 s after the end
        self.assertEqual(rs.next_delay(60, 0), 60)
        self.assertEqual(rs.next_delay(60, 400), 1200)

    def test_baseline_due_once_a_night_local(self):
        tz = "Europe/Bratislava"                              # 03:30 local = 01:30 UTC in October (CEST)
        early = datetime(2026, 10, 17, 1, 0, tzinfo=timezone.utc)
        late = datetime(2026, 10, 17, 1, 31, tzinfo=timezone.utc)
        self.assertEqual(rs.baseline_due(early, tz, None), (False, "2026-10-17"))
        self.assertEqual(rs.baseline_due(late, tz, None), (True, "2026-10-17"))
        self.assertEqual(rs.baseline_due(late, tz, "2026-10-17"), (False, "2026-10-17"))
        self.assertEqual(rs.baseline_due(late, tz, "2026-10-16"), (True, "2026-10-17"))


if __name__ == "__main__":
    unittest.main()
