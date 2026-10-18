"""Tests for analysis/clock_validation.py on a synthetic v8 buffer: a boot
6 h behind whose clock is stepped by NTP after 30 min, a boot that never got
an anchor, and a truth file sampled on the monotonic clock.
Run from wifi-sensor/:  python3 -m unittest discover -s analysis/tests
"""

import contextlib
import csv
import io
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import clock_validation as cv  # noqa: E402

E = 1_792_300_000.0          # true epoch of boot A (wall at mono 0)
BEHIND = 6 * 3600.0
STEP_MONO = 1800.0           # chrony online after 30 min


def make_buffer(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '8');
        CREATE TABLE polls (id INTEGER PRIMARY KEY, ts INTEGER, mono REAL, boot_id TEXT, clock_synced INTEGER);
        CREATE TABLE clock_anchors (boot_id TEXT PRIMARY KEY, mono REAL, wall REAL, poll_id INTEGER,
                                    source TEXT DEFAULT 'ntp');
    """)
    mono = 15.3
    while mono < 2400:
        synced = mono >= STEP_MONO
        wall = E + mono if synced else E - BEHIND + mono
        db.execute("INSERT INTO polls (ts, mono, boot_id, clock_synced) VALUES (?, ?, 'A', ?)",
                   (int(wall), mono, 1 if synced else 0))
        if synced and not db.execute("SELECT 1 FROM clock_anchors WHERE boot_id = 'A'").fetchone():
            db.execute("INSERT INTO clock_anchors VALUES ('A', ?, ?, ?, 'ntp')",
                       (mono, E + mono + 0.004, db.execute("SELECT max(id) FROM polls").fetchone()[0]))
        mono += 30.0
    for i in range(5):              # boot B: rebooted before any anchor (run B)
        db.execute("INSERT INTO polls (ts, mono, boot_id, clock_synced) VALUES (?, ?, 'B', 0)",
                   (int(E - BEHIND + 9000 + 30 * i), 20.0 + 30 * i))
    db.commit()
    db.close()


def make_truth(path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mac_t0", "mac_t1", "true_ts", "half_rtt_s", "pi_mono", "pi_wall", "pi_wall_err_s",
                    "boot_id", "status"])
        w.writerow(["", "", "", "", "", "", "", "", "missed: session closed"])
        for k in range(0, 41):
            mono = 60.0 * k + 0.7
            true = E + mono + (0.01 if k % 2 else -0.01)          # Mac timing noise
            wall = E + mono if mono >= STEP_MONO else E - BEHIND + mono
            w.writerow([true - 0.01, true + 0.01, true, 0.01, mono, wall, wall - true, "A", "ok"])


class ClockValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "buffer.db")
        self.truth = os.path.join(self.tmp, "truth.csv")
        self.out = os.path.join(self.tmp, "errors.csv")
        make_buffer(self.db)
        make_truth(self.truth)

    def run_cv(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cv.main([self.db, "--state", os.path.join(self.tmp, "none.json"),
                     "--uploader-dir", os.path.join(os.path.dirname(os.path.dirname(HERE)), "uploader"), *args])
        return buf.getvalue()

    def test_run_a_errors_are_small(self):
        out = self.run_cv("--truth", self.truth, "--csv", self.out)
        self.assertIn("offset applied +21600.", out)          # stale epoch from an integer ts
        self.assertIn("PASS", out)
        with open(self.out) as f:
            errs = [abs(float(r["error_s"])) for r in csv.DictReader(f)]
        self.assertEqual(len(errs), 41)
        self.assertLess(max(errs), 0.02)          # anchor 4 ms + truth noise 10 ms
        line = [l for l in out.splitlines() if "sent ts - true" in l][0]
        self.assertLess(float(line.split("max |err| ")[1].split(" s")[0]), 1.0)   # integer ts

    def test_unverified_boot_is_listed(self):
        out = self.run_cv("--unverified")
        self.assertIn("B: 5 polls", out)
        self.assertIn("UNVERIFIED", out)

    def test_compare_anchors_needs_both(self):
        out = self.run_cv("--truth", self.truth, "--compare-anchors")
        self.assertIn("needs both an NTP and a server anchor (have: ntp)", out)


if __name__ == "__main__":
    unittest.main()
