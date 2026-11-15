"""Tests for analysis/extract_poll_rows.py on a buffer written by the real
collector store: the AP rows of the asked polls, nothing of a client.
Run from wifi-sensor/:  python3 -m unittest discover -s analysis/tests
"""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector", "tests"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "uploader"))

import batch  # noqa: E402
import extract_poll_rows as epr  # noqa: E402
from shape import shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import AP_RAW, CLIENT_RAW, TS  # noqa: E402

HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
BOOT = "33333333-3333-4333-8333-333333333333"


class Extract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "buffer.db")
        store = Store(self.path, boot_id=BOOT)
        for i in range(3):                                   # polls 1..3 on a stale clock, an AP and a client each
            ts = TS + 30 * i
            recs = []
            for raw in (AP_RAW, CLIENT_RAW):
                r = dict(raw)
                r["kismet.device.base.last_time"] = raw["kismet.device.base.last_time"] + 30 * i
                recs.append(shape_device(r, ts))
            store.write_poll(ts, HEALTH, recs, [], 5,
                             clock={"boot_id": BOOT, "mono": 100.0 + 30 * i, "synced": False, "chrony_offset": 0.0,
                                    "wall": None})
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = epr.main(["--db", self.path] + [str(a) for a in args])
        return rc, out.getvalue(), err.getvalue()

    def test_the_ap_rows_of_the_asked_polls(self):
        rc, out, err = self.run_main(2, 3)
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual(got["format"], epr.FORMAT)
        self.assertEqual([(p["id"], p["ts"], p["clock_synced"]) for p in got["polls"]],
                         [(2, TS + 30, 0), (3, TS + 60, 0)])
        self.assertEqual([(o["poll_id"], o["ts"], o["key"]) for o in got["observations"]],
                         [(2, TS + 30, AP_RAW["kismet.device.base.key"]), (3, TS + 60, AP_RAW["kismet.device.base.key"])])
        self.assertNotIn(CLIENT_RAW["kismet.device.base.key"], out)              # no client row leaves the sensor
        self.assertEqual(set(got["observations"][0]), set(epr.OBS_COLS))
        self.assertIn("poll 2: 1 AP rows, poll 3: 1 AP rows", err)

    def test_a_poll_that_is_gone_is_an_error_not_an_empty_extract(self):
        with self.assertRaises(SystemExit) as e:
            self.run_main(2, 99)
        self.assertIn("poll 99", str(e.exception))

    def test_the_buffer_is_not_written(self):
        before = os.stat(self.path).st_mtime_ns
        self.run_main(1)
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 6)
        db.close()

    def test_the_ap_rule_is_the_uploaders(self):
        self.assertEqual(epr.AP_EVIDENCE, batch.AP_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
