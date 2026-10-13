"""The SQL files of the v2 endpoints (app/v2/sql, app/v2/queries.py). Standard library only.

The statements themselves run against the real data in tests/perf/ep_v2.py
(EXPLAIN, read-only); here: every file parses, uses only named placeholders,
and every query a route asks for exists.
"""

import glob
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import queries  # noqa: E402

ROUTES = os.path.join(os.path.dirname(__file__), "..", "app", "v2")


class SqlFiles(unittest.TestCase):
    def groups(self):
        return [os.path.basename(p)[:-4] for p in glob.glob(os.path.join(queries.SQL_DIR, "*.sql"))]

    def test_every_file_parses_with_named_placeholders_only(self):
        self.assertIn("context", self.groups())
        for g in self.groups():
            qs = queries.load(g)
            self.assertTrue(qs, g)
            for name, sql in qs.items():
                with self.subTest(query="%s.%s" % (g, name)):
                    bare = queries.PARAM.sub("", sql).replace("%%", "")
                    self.assertNotIn("%", bare, "positional or stray % placeholder")
                    self.assertFalse(sql.rstrip().endswith(";"))
                    # writes.sql holds the write statements (phase 5), every other file reads
                    kinds = r"(WITH|INSERT|UPDATE|DELETE)" if g == "writes" else r"(WITH|SELECT)"
                    self.assertTrue(re.match(r"^(--[^\n]*\n)*\s*" + kinds + r"\b", sql, re.S),
                                    "a write statement" if g == "writes" else "a read statement")

    def test_the_perf_tool_never_runs_the_writes(self):
        # tests/perf/ep_v2.py runs every query of its groups under EXPLAIN ANALYZE
        path = os.path.join(os.path.dirname(__file__), "..", "..", "tests", "perf", "ep_v2.py")
        if not os.path.exists(path):              # the api image has only api/
            self.skipTest("tests/perf not here")
        src = open(path).read()
        groups = re.search(r"^GROUPS = \((.*?)\)", src, re.M | re.S).group(1)
        self.assertNotIn('"writes"', groups)
        self.assertIn("writes", self.groups())

    def test_every_query_a_route_runs_exists(self):
        used = 0
        for path in glob.glob(os.path.join(ROUTES, "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            loads = dict(re.findall(r"^(_Q\w*) = queries\.load\(\"(\w+)\"\)", src, re.M))
            for var, name in re.findall(r"run\(conn, (_Q\w*), \"(\w+)\"", src):
                used += 1
                with self.subTest(route=os.path.basename(path), query=name):
                    self.assertIn(var, loads)
                    self.assertIn(name, queries.load(loads[var]))
        self.assertGreater(used, 0)

    def test_parse_and_to_psql(self):
        qs = queries.parse("-- file comment\n-- name: a\nSELECT %(x)s::int, '%%' AS p;\n\n-- name: b_2\nSELECT 1\n")
        self.assertEqual(list(qs), ["a", "b_2"])
        self.assertEqual(queries.params(qs["a"]), ["x"])
        self.assertEqual(queries.to_psql(qs["a"]), "SELECT :'x'::int, '%' AS p")
        with self.assertRaises(ValueError):
            queries.parse("-- name: a\nSELECT 1\n-- name: a\nSELECT 2\n")

    def test_fragments(self):
        qs = queries.parse("-- name: _f\nWHERE x = %(x)s\n-- name: a\nSELECT 1 FROM t {{_f}}\n"
                           "-- name: b\nSELECT count(*) FROM t {{_f}} AND y\n")
        self.assertEqual(qs, {"a": "SELECT 1 FROM t WHERE x = %(x)s",
                              "b": "SELECT count(*) FROM t WHERE x = %(x)s AND y"})
        with self.assertRaises(ValueError):
            queries.parse("-- name: a\nSELECT 1 {{_missing}}\n")


if __name__ == "__main__":
    unittest.main()
