"""ETag logic of the read endpoints (app/cache.py). Standard library only:

  cd server && python3 -m unittest discover -s api/tests
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import cache  # noqa: E402

T = datetime(2026, 9, 29, 20, 39, 10, 123456, tzinfo=timezone.utc)


class RollupEtag(unittest.TestCase):
    def test_weak_and_versioned_by_the_refresh(self):
        tag = cache.rollup_etag(1, T)
        self.assertTrue(tag.startswith('W/"') and tag.endswith('"'))
        self.assertNotEqual(tag, cache.rollup_etag(1, T + timedelta(microseconds=1)))
        self.assertNotEqual(tag, cache.rollup_etag(2, T))

    def test_no_etag_before_the_first_refresh(self):
        self.assertIsNone(cache.rollup_etag(1, None))
        self.assertEqual(cache.headers(None), {"Cache-Control": cache.CACHE_CONTROL})

    def test_extra_values_are_part_of_it(self):
        base = cache.rollup_etag(1, T, 8, 8, 1758000000)
        self.assertNotEqual(base, cache.rollup_etag(1, T, 8, 7, 1758000000))   # one acked
        self.assertNotEqual(cache.rollup_etag(1, T, 0, 0, None), cache.rollup_etag(1, T, 0, 0, 0))

    def test_only_digits_dashes_and_x_inside_the_quotes(self):
        tag = cache.rollup_etag(1, T, None, 3)
        self.assertRegex(tag, r'^W/"[0-9x-]+"$')


class Matches(unittest.TestCase):
    def setUp(self):
        self.tag = cache.rollup_etag(1, T)

    def test_exact_and_weak_comparison(self):
        self.assertTrue(cache.matches(self.tag, self.tag))
        self.assertTrue(cache.matches(self.tag[2:], self.tag))          # strong form of the same tag
        self.assertTrue(cache.matches('W/"other", ' + self.tag, self.tag))
        self.assertTrue(cache.matches("*", self.tag))

    def test_no_match(self):
        self.assertFalse(cache.matches(None, self.tag))
        self.assertFalse(cache.matches("", self.tag))
        self.assertFalse(cache.matches('W/"1-2"', self.tag))
        self.assertFalse(cache.matches(self.tag, None))                  # nothing to compare with

    def test_headers(self):
        self.assertEqual(cache.headers(self.tag),
                         {"Cache-Control": cache.CACHE_CONTROL, "ETag": self.tag})


if __name__ == "__main__":
    unittest.main()
