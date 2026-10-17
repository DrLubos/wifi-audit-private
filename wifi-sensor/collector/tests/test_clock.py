"""clock.py: the boot id and chrony's synchronisation state (buffer v8).
Standard library only."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import clock  # noqa: E402

# chronyc -c tracking on the Pi (2026-10-17) and while unsynchronised
SYNCED = ("5AB01500,90.176.21.0,2,1792250668.058849174,0.000377963,-0.000303116,0.000284493,"
          "1.774,-0.000,0.057,0.006979416,0.001247354,64.3,Normal\n")
UNSYNCED = ("00000000,,0,0.000000000,0.000000000,0.000000000,0.000000000,0.000,0.000,0.000,"
            "1.000000000,1.000000000,0.0,Not synchronised\n")
SLEWING = SYNCED.replace("0.000377963", "-2.500000000")


def runner(stdout, code=0, exc=None):
    def run(args, **kw):
        if exc:
            raise exc
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")
    return run


class Chrony(unittest.TestCase):
    def test_synced(self):
        self.assertEqual(clock.chrony_state(runner(SYNCED)), (True, 0.000377963))

    def test_not_synchronised(self):
        self.assertEqual(clock.chrony_state(runner(UNSYNCED)), (False, 0.0))

    def test_large_offset_is_not_synced(self):
        # Normal leap status while a large offset is still being slewed away
        self.assertEqual(clock.chrony_state(runner(SLEWING)), (False, -2.5))

    def test_failures(self):
        for run in (runner("", code=1), runner("garbage\n"),
                    runner("", exc=FileNotFoundError("chronyc")),
                    runner("", exc=subprocess.TimeoutExpired("chronyc", 3))):
            self.assertEqual(clock.chrony_state(run), (False, None))


class Boot(unittest.TestCase):
    def test_boot_id(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("c5755a69-7e4c-4709-86fc-41d7dd8bad86\n")
        try:
            self.assertEqual(clock.boot_id(f.name), "c5755a69-7e4c-4709-86fc-41d7dd8bad86")
        finally:
            os.unlink(f.name)
        self.assertIsNone(clock.boot_id("/nonexistent/boot_id"))

    def test_now_reads_both_clocks(self):
        wall, mono = clock.now()
        self.assertGreater(wall, 1.7e9)
        self.assertGreater(mono, 0)


if __name__ == "__main__":
    unittest.main()
