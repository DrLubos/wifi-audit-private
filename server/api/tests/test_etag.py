"""The v2 ETag (app/cache.py v2_etag, PLAN.md 7.1). Standard library only."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import cache  # noqa: E402
from app.v2 import periods  # noqa: E402

T = datetime(2026, 10, 11, 14, 8, 52, 299878, tzinfo=timezone.utc)
E = datetime(2026, 10, 11, 13, 49, 32, 927121, tzinfo=timezone.utc)


class V2Etag(unittest.TestCase):
    def test_every_part_changes_the_tag(self):
        base = cache.v2_etag(1, T, 3, E, 1760180400)
        self.assertTrue(base.startswith('W/"v2-1-') and base.endswith('"'))
        others = [cache.v2_etag(2, T, 3, E, 1760180400),
                  cache.v2_etag(1, T + timedelta(microseconds=1), 3, E, 1760180400),
                  cache.v2_etag(1, T, 4, E, 1760180400),
                  cache.v2_etag(1, T, 3, E + timedelta(microseconds=1), 1760180400),
                  cache.v2_etag(1, T, 3, None, 1760180400),
                  cache.v2_etag(1, T, 3, E, 1760184000),
                  cache.v2_etag(1, T, 3, E, None),
                  cache.v2_etag(1, T, 3, E, 1760180400, 5)]
        self.assertEqual(len(set(others + [base])), len(others) + 1)

    def test_none_before_the_first_refresh(self):
        self.assertIsNone(cache.v2_etag(1, None, 0, None, None))

    def test_matches_weakly(self):
        tag = cache.v2_etag(1, T, 0, E, 1760180400)
        self.assertTrue(cache.matches(tag, tag))
        self.assertTrue(cache.matches(tag[2:], tag))

    def test_the_bucket_epoch_moves_with_the_bucket(self):
        tz = "Europe/Bratislava"
        at = lambda *a: periods.period("7d", tz, datetime(*a, tzinfo=timezone.utc)).epoch  # noqa: E731
        # 12:00-18:00 local = 10:00-16:00 UTC on 10 Oct
        self.assertEqual(cache.v2_etag(1, T, 0, E, at(2026, 10, 10, 10, 1)),
                         cache.v2_etag(1, T, 0, E, at(2026, 10, 10, 15, 59)))
        self.assertNotEqual(cache.v2_etag(1, T, 0, E, at(2026, 10, 10, 15, 59)),
                            cache.v2_etag(1, T, 0, E, at(2026, 10, 10, 16, 0)))


if __name__ == "__main__":
    unittest.main()
