"""Buffer schema v9 (configuration delivery, server/docs/config-delivery.md
6): every poll records the configuration version it was captured with
(polls.config_version, from the applier's applied.json; NULL when none was
ever applied). Standard library only."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shape import shape_device  # noqa: E402
from store import SCHEMA_VERSION, Store, applied_config_version  # noqa: E402
from test_shape import AP_RAW, TS  # noqa: E402

HEALTH = {"kismet_ts": TS, "devices_total": 1, "ds_running": 1, "ds_error": None, "ds_packets": 1000}


class V9(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "buffer.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_is_9(self):
        self.assertEqual(SCHEMA_VERSION, 9)

    def test_poll_rows_carry_the_configuration_version(self):
        s = Store(self.path)
        s.write_poll(TS, dict(HEALTH, config_version=4), [shape_device(AP_RAW, TS)], [], 42)
        s.write_poll(TS + 30, HEALTH, [], [], 42)
        s.write_failed_poll(TS + 60, "boom", 5, config_version=4)
        self.assertEqual(s.db.execute("SELECT config_version FROM polls ORDER BY id").fetchall(),
                         [(4,), (None,), (4,)])
        s.close()

    def test_v8_to_v9(self):
        s = Store(self.path)
        s.write_poll(TS, HEALTH, [shape_device(AP_RAW, TS)], [], 42)
        s.close()
        db = sqlite3.connect(self.path)
        db.execute("ALTER TABLE polls DROP COLUMN config_version")
        db.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")
        db.commit()
        db.close()
        s = Store(self.path)
        self.assertEqual(s.get_meta("schema_version"), "9")
        self.assertEqual(s.db.execute("SELECT config_version FROM polls").fetchall(), [(None,)])
        s.close()


class AppliedVersion(unittest.TestCase):
    def test_read_from_the_appliers_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "applied.json")
            self.assertIsNone(applied_config_version(p))                 # nothing applied yet
            with open(p, "w") as f:
                json.dump({"version": 7, "sha256": "a" * 64}, f)
            self.assertEqual(applied_config_version(p), 7)
            for bad in ("not json", json.dumps({"version": "7"}), json.dumps({"version": -1}), json.dumps([7])):
                with open(p, "w") as f:
                    f.write(bad)
                self.assertIsNone(applied_config_version(p))
            self.assertIsNone(applied_config_version(None))


if __name__ == "__main__":
    unittest.main()
