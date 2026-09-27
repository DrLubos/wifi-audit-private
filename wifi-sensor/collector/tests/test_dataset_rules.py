"""Tests for dataset_rules.py and its server twin (server/schema.sql rssi_valid)."""

import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataset_rules import (RSSI_FLOOR_DBM, is_rssi_floor, rssi_value,  # noqa: E402
                           sql_rssi, sql_rssi_is_floor)

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "..", "..", "..", "server", "schema.sql")


class RulesTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(RSSI_FLOOR_DBM, {-106, -120})
        self.assertIsNone(rssi_value(-106))
        self.assertIsNone(rssi_value(-120))
        self.assertIsNone(rssi_value(None))
        self.assertEqual(rssi_value(-105), -105)
        self.assertTrue(is_rssi_floor(-106))
        self.assertFalse(is_rssi_floor(-107))
        self.assertFalse(is_rssi_floor(None))

    def test_sql_helpers_match_python(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE o (rssi INTEGER, rssi_floor INTEGER)")
        db.executemany("INSERT INTO o VALUES (?, ?)",
                       [(-60, None), (-106, None), (-120, None), (None, 1), (None, None), (-105, None)])
        rows = db.execute("SELECT rssi, %s, %s FROM o" % (sql_rssi(), sql_rssi_is_floor("rssi", "rssi_floor"))).fetchall()
        self.assertEqual([r[1] for r in rows], [-60, None, None, None, None, -105])
        self.assertEqual([r[2] for r in rows], [0, 1, 1, 1, 0, 0])

    def test_server_schema_uses_the_same_floor_values(self):
        """One rule, two languages: server/schema.sql rssi_valid() must list the
        same floors as RSSI_FLOOR_DBM."""
        if not os.path.isfile(SCHEMA_SQL):
            self.skipTest("server/schema.sql not present (sensor-only checkout)")
        text = open(SCHEMA_SQL).read()
        m = re.search(r"FUNCTION rssi_valid\b.*?IN \(([^)]*)\)", text, re.S)
        self.assertIsNotNone(m, "rssi_valid() not found in server/schema.sql")
        server = {int(v) for v in m.group(1).split(",")}
        self.assertEqual(server, set(RSSI_FLOOR_DBM))


if __name__ == "__main__":
    unittest.main()
