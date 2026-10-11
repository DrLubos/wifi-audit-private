"""The v2 response cache (app/v2/respcache.py). Standard library only."""

import json
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import respcache  # noqa: E402

T = datetime(2026, 10, 11, 20, 3, 15, tzinfo=timezone.utc)


class Lru(unittest.TestCase):
    def test_bounded_by_bytes_least_recently_used_first(self):
        c = respcache.LRU(max_bytes=40)
        self.assertTrue(c.put("a", "t1", b"x" * 10))
        c.put("b", "t1", b"x" * 10)
        c.put("c", "t1", b"x" * 10)
        self.assertIsNotNone(c.get("a", "t1"))            # a is now the most recent
        c.put("d", "t1", b"x" * 10)
        c.put("e", "t1", b"x" * 10)                       # 50 > 40: b goes first
        self.assertIsNone(c.get("b", "t1"))
        self.assertIsNotNone(c.get("a", "t1"))
        self.assertLessEqual(c.bytes, 40)
        self.assertEqual(c.bytes, sum(len(c.get(k, "t1")) for k in ("a", "c", "d", "e") if c.get(k, "t1")))

    def test_the_tag_is_part_of_the_key(self):
        c = respcache.LRU(max_bytes=100)
        c.put("/x", "W/\"v2-1\"", b"{}")
        self.assertIsNone(c.get("/x", "W/\"v2-2\""))       # a refresh moved the tag: a miss
        self.assertEqual(c.get("/x", "W/\"v2-1\""), b"{}")

    def test_oversized_bodies_are_not_kept(self):
        c = respcache.LRU(max_bytes=40)
        self.assertFalse(c.put("big", "t", b"x" * 11))     # > a quarter of the budget
        self.assertEqual(len(c), 0)

    def test_replacing_an_entry_keeps_the_byte_count(self):
        c = respcache.LRU(max_bytes=100)
        c.put("a", "t", b"x" * 10)
        c.put("a", "t", b"x" * 20)
        self.assertEqual((len(c), c.bytes), (1, 20))


class Render(unittest.TestCase):
    def setUp(self):
        respcache.cache.clear()

    def test_request_key_ignores_parameter_order(self):
        self.assertEqual(respcache.request_key("/api/v2/aps", [("range", "7d"), ("band", "5")]),
                         respcache.request_key("/api/v2/aps", [("band", "5"), ("range", "7d")]))
        self.assertEqual(respcache.request_key("/api/v2/sensors", []), "/api/v2/sensors")

    def test_a_hit_gets_the_envelope_of_its_own_request(self):
        env1 = {"filters": {"sensor": "all", "range": "30d"}, "period": {"to": "2026-10-11T20:00:00Z"}}
        env2 = {"filters": {"sensor": "all", "range": "30d"}, "period": {"to": "2026-10-11T23:59:00Z"}}
        body = dict(env1, account={"name": "a"}, buckets=[{"start": T}])
        tag1 = respcache.make_tag('W/"v2-1"', "/p?range=30d", env1)
        first = json.loads(respcache.render(body, tag1))
        self.assertEqual(first["period"]["to"], "2026-10-11T20:00:00Z")
        self.assertEqual(first["buckets"][0]["start"], "2026-10-11T20:03:15Z")
        hit = json.loads(respcache.lookup(respcache.make_tag('W/"v2-1"', "/p?range=30d", env2)))
        self.assertEqual(hit["period"]["to"], "2026-10-11T23:59:00Z")       # not the cached request time
        self.assertEqual(hit["account"], {"name": "a"})
        self.assertEqual(list(hit), ["filters", "period", "account", "buckets"])
        self.assertIsNone(respcache.lookup(respcache.make_tag('W/"v2-2"', "/p?range=30d", env2)))

    def test_bodies_without_an_envelope_are_kept_whole(self):
        tag = respcache.make_tag('W/"v2-1"', "/events?limit=5")
        body = {"filters": {"sensor": "all"}, "items": [1, 2]}
        self.assertEqual(json.loads(respcache.render(body, tag)), body)
        self.assertEqual(json.loads(respcache.lookup(tag)), body)

    def test_no_tag_no_cache(self):
        self.assertIsNone(respcache.make_tag(None, "/x"))
        self.assertEqual(json.loads(respcache.render({"a": 1}, None)), {"a": 1})
        self.assertIsNone(respcache.lookup(None))
        self.assertEqual(len(respcache.cache), 0)

    def test_join(self):
        self.assertEqual(respcache.join({"a": 1}, b'{"b":2}'), b'{"a":1,"b":2}')
        self.assertEqual(respcache.join({"a": 1}, b"{}"), b'{"a":1}')
        self.assertEqual(respcache.join(None, b'{"b":2}'), b'{"b":2}')


if __name__ == "__main__":
    unittest.main()
