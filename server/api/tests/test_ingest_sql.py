"""The ingest's SQL (app/ingest/sql/ingest.sql) and its use in routes.py.
Standard library only."""

import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import queries  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app", "ingest")
SQL = queries.parse(open(os.path.join(APP, "sql", "ingest.sql"), encoding="utf-8").read())
ROUTES = open(os.path.join(APP, "routes.py"), encoding="utf-8").read()


class IngestSql(unittest.TestCase):
    def test_every_query_used_exists_and_every_query_is_used(self):
        used = set(re.findall(r'_run\(conn, "(\w+)"', ROUTES))
        self.assertEqual(used - set(SQL), set())
        self.assertEqual(set(SQL) - used, set())

    def test_parameters_are_passed(self):
        # every _run(conn, "q", a=..., ...) passes each %(param)s of q (a **dict covers the rest)
        calls = 0
        for node in ast.walk(ast.parse(ROUTES)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_run"
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
                calls += 1
                name = node.args[1].value
                if any(k.arg is None for k in node.keywords):
                    continue
                passed = {k.arg for k in node.keywords}
                self.assertEqual(set(queries.params(SQL[name])) - passed, set(), name)
                self.assertEqual(passed - set(queries.params(SQL[name])), set(), name)
        self.assertEqual(calls, len(re.findall(r'_run\(conn, "', ROUTES)))

    def test_merge_goes_through_the_shared_function(self):
        self.assertIn("ingest_merge(%(sensor_id)s, 'ingest')", SQL["merge"])
        self.assertIn("SELECT ingest_stage_create()", SQL["stage_create"])
        for q in SQL.values():
            for t in ("observations", "polls ", "devices "):
                self.assertNotRegex(q, r"INSERT INTO %s" % t)     # only ingest_merge() writes them

    def test_no_client_table_and_no_write_token(self):
        text = " ".join(SQL.values())
        for t in ("associations", "client_bssids", "probes"):
            self.assertNotIn(t, text)
        self.assertNotIn("WRITE_TOKEN", ROUTES)

    def test_stall_events_are_sensor_kind_high(self):
        self.assertIn("'sensor', %(ts)s::timestamptz, %(type)s::text, 'high'", SQL["event_open"])
        self.assertIn("d.ts_end IS NULL", SQL["event_open"])


if __name__ == "__main__":
    unittest.main()
