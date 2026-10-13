"""ETag logic of the dashboard endpoints (app/cache.py). Standard library only:

  cd server && python3 -m unittest discover -s api/tests
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import cache  # noqa: E402

T = datetime(2026, 9, 29, 20, 39, 10, 123456, tzinfo=timezone.utc)


class V2Etag(unittest.TestCase):
    def test_weak_and_versioned_by_the_refresh(self):
        tag = cache.v2_etag(1, T, 0, None, None)
        self.assertTrue(tag.startswith('W/"') and tag.endswith('"'))
        self.assertNotEqual(tag, cache.v2_etag(1, T + timedelta(microseconds=1), 0, None, None))
        self.assertNotEqual(tag, cache.v2_etag(2, T, 0, None, None))
        self.assertNotEqual(tag, cache.v2_etag(1, T, 1, None, None))                 # a write

    def test_no_etag_before_the_first_refresh(self):
        self.assertIsNone(cache.v2_etag(1, None, 0, None, None))
        self.assertEqual(cache.headers(None), {"Cache-Control": cache.CACHE_CONTROL})

    def test_only_digits_dashes_v_and_x_inside_the_quotes(self):
        self.assertRegex(cache.v2_etag(1, T, 3, T, 7, None, 2), r'^W/"[0-9vx-]+"$')


class Matches(unittest.TestCase):
    def setUp(self):
        self.tag = cache.v2_etag(1, T, 0, None, None)

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
