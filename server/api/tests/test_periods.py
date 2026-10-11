"""Buckets of the v2 period endpoints (app/v2/periods.py). Standard library only:

  cd server && python3 -m unittest discover -s api/tests
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import periods  # noqa: E402

TZ = "Europe/Bratislava"
UTC = timezone.utc


def utc(*a):
    return datetime(*a, tzinfo=UTC)


class Ranges(unittest.TestCase):
    def check_contiguous(self, p):
        self.assertEqual(p.starts[1:], p.ends[:-1])
        for t in p.starts + p.ends:
            self.assertEqual((t.minute, t.second, t.microsecond), (0, 0, 0))
            self.assertEqual(t.tzinfo, UTC)
        self.assertLessEqual(p.starts[-1], p.now)
        self.assertLess(p.now, p.ends[-1])

    def test_24h(self):
        # Sat 10 Oct 2026 17:52 local = 15:52 UTC (CEST, +2)
        p = periods.period("24h", TZ, utc(2026, 10, 10, 15, 52, 30))
        self.assertEqual(len(p.starts), 24)
        self.assertEqual(p.starts[0], utc(2026, 10, 9, 16))       # Fri 18:00 local
        self.assertEqual(p.starts[-1], utc(2026, 10, 10, 15))
        self.assertEqual(p.bucket, "1h")
        self.check_contiguous(p)
        self.assertEqual(p.seconds(23), 52 * 60 + 30)               # the current hour is partial
        self.assertEqual(p.seconds(0), 3600)
        self.assertEqual(p.label(), "last 24 hours: 9 Oct 18:00 – 10 Oct 17:52")

    def test_7d_aligned_to_local_quarters(self):
        p = periods.period("7d", TZ, utc(2026, 10, 10, 15, 52))
        self.assertEqual(len(p.starts), 28)
        self.assertEqual(p.starts[-1], utc(2026, 10, 10, 10))      # 12:00 local
        self.assertEqual(p.ends[-1], utc(2026, 10, 10, 16))        # 18:00 local
        self.assertEqual(p.starts[0], utc(2026, 10, 3, 16))        # 3 Oct 18:00 local
        self.check_contiguous(p)
        self.assertTrue(all(e - s == timedelta(hours=6) for s, e in zip(p.starts, p.ends)))
        self.assertEqual(p.label(), "last 7 days: 3 Oct 18:00 – 10 Oct 17:52")

    def test_7d_at_a_bucket_edge(self):
        p = periods.period("7d", TZ, utc(2026, 10, 10, 16, 0))     # 18:00 local exactly
        self.assertEqual(p.starts[-1], utc(2026, 10, 10, 16))
        self.assertEqual(p.seconds(27), 0)

    def test_30d_calendar_days(self):
        p = periods.period("30d", TZ, utc(2026, 10, 10, 15, 52))
        self.assertEqual(len(p.starts), 30)
        self.assertEqual(p.starts[-1], utc(2026, 10, 9, 22))       # 10 Oct 00:00 local
        self.assertEqual(p.starts[0], utc(2026, 9, 10, 22))        # 11 Sep 00:00 local
        self.check_contiguous(p)
        self.assertEqual(p.bucket, "1d")

    def test_dst_end_2026_10_25(self):
        # 25 Oct 2026 03:00 CEST -> 02:00 CET: the local day has 25 hours
        p = periods.period("30d", TZ, utc(2026, 10, 30, 12))
        i = p.starts.index(utc(2026, 10, 24, 22))                 # 25 Oct 00:00 CEST
        self.assertEqual(p.ends[i], utc(2026, 10, 25, 23))        # 26 Oct 00:00 CET
        self.assertEqual(p.ends[i] - p.starts[i], timedelta(hours=25))
        self.assertEqual(p.starts[-1], utc(2026, 10, 29, 23))
        self.check_contiguous(p)
        q = periods.period("7d", TZ, utc(2026, 10, 27, 12))
        j = q.starts.index(utc(2026, 10, 24, 22))                 # 25 Oct 00:00 -> 06:00 is 7 h
        self.assertEqual(q.ends[j] - q.starts[j], timedelta(hours=7))
        self.assertEqual(q.ends[j], utc(2026, 10, 25, 5))
        self.check_contiguous(q)

    def test_dst_start_2027_03_28(self):
        # 28 Mar 2027 02:00 CET -> 03:00 CEST: 23-hour day, 5-hour first window
        p = periods.period("30d", TZ, utc(2027, 4, 2, 12))
        i = p.starts.index(utc(2027, 3, 27, 23))                  # 28 Mar 00:00 CET
        self.assertEqual(p.ends[i] - p.starts[i], timedelta(hours=23))
        q = periods.period("7d", TZ, utc(2027, 3, 30, 12))
        j = q.starts.index(utc(2027, 3, 27, 23))
        self.assertEqual(q.ends[j] - q.starts[j], timedelta(hours=5))
        self.check_contiguous(q)

    def test_epoch_changes_with_the_bucket(self):
        a = periods.period("24h", TZ, utc(2026, 10, 10, 15, 0, 1))
        b = periods.period("24h", TZ, utc(2026, 10, 10, 15, 59, 59))
        c = periods.period("24h", TZ, utc(2026, 10, 10, 16, 0, 0))
        self.assertEqual(a.epoch, b.epoch)
        self.assertNotEqual(b.epoch, c.epoch)
        d = periods.period("30d", TZ, utc(2026, 10, 10, 21, 59))   # 23:59 local
        e = periods.period("30d", TZ, utc(2026, 10, 10, 22, 0))    # midnight local
        self.assertNotEqual(d.epoch, e.epoch)

    def test_envelope(self):
        p = periods.period("7d", TZ, utc(2026, 10, 10, 15, 52, 7, 500))
        env = p.envelope("all")
        self.assertEqual(env["filters"], {"sensor": "all", "range": "7d"})
        self.assertEqual(env["period"]["from"], "2026-10-03T16:00:00Z")
        self.assertEqual(env["period"]["to"], "2026-10-10T15:52:07Z")
        self.assertEqual(env["period"]["tz"], TZ)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            periods.period("90d", TZ, utc(2026, 10, 10))
        with self.assertRaises(ValueError):
            periods.period("24h", TZ, datetime(2026, 10, 10))
        with self.assertRaises(ValueError):                        # half-hour offset
            periods.period("30d", "Asia/Kolkata", utc(2026, 10, 10))

    def test_days_window(self):
        p = periods.days_window(TZ, utc(2026, 10, 10, 15, 52), 28)
        self.assertEqual(len(p.starts), 28)
        self.assertEqual(p.starts[-1], utc(2026, 10, 9, 22))       # today 00:00 local
        self.assertEqual(p.starts[0], utc(2026, 9, 12, 22))
        self.check_contiguous(p)
        self.assertEqual(p.label(), "last 28d: 13 Sep 00:00 – 10 Oct 17:52")

    def test_local_days(self):
        days = periods.local_days(TZ, utc(2026, 10, 25, 12), 3)
        self.assertEqual([d for d, _, _ in days], [date(2026, 10, 23), date(2026, 10, 24), date(2026, 10, 25)])
        self.assertEqual(days[-1][1:], (utc(2026, 10, 24, 22), utc(2026, 10, 25, 23)))


if __name__ == "__main__":
    unittest.main()
