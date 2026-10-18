"""Tests for sensor/clock-test.sh (docs/clock-validation.md) against stub
chronyc / systemctl / systemd-run in a temporary tree - nothing touches the
real clock file, chrony or systemd.

The script needs GNU date/touch/stat (the Pi); elsewhere the tests skip.
Run from wifi-sensor/:  python3 -m unittest discover -s sensor/tests
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "clock-test.sh"))


def gnu_tools():
    try:
        return subprocess.run(["date", "-u", "-d", "@0", "+%s"], capture_output=True, text=True).stdout.strip() == "0"
    except OSError:
        return False


STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{calls}"
case "$(basename "$0") $1" in
  "chronyc -c") cat "{leap}" ;;
  "chronyc offline") n=$(cut -d. -f1 "{uptime}"); echo "$((n + 30)).00 0.00" > "{uptime}" ;;
esac
exit 0
"""


@unittest.skipUnless(gnu_tools(), "needs GNU date/touch/stat (run on the Pi)")
class ClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        j = lambda *p: os.path.join(self.tmp, *p)  # noqa: E731
        self.bin, self.calls, self.leap, self.uptime = j("bin"), j("calls"), j("leap"), j("uptime")
        os.makedirs(self.bin)
        os.makedirs(j("timesync"))
        self.clock = j("timesync", "clock")
        open(self.clock, "w").close()
        for name in ("chronyc", "systemctl", "systemd-run"):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(STUB.format(calls=self.calls, leap=self.leap, uptime=self.uptime))
            os.chmod(os.path.join(self.bin, name), 0o755)
        self.saved = j("clock-save-ran")
        with open(j("clock-save.sh"), "w") as f:
            f.write("#!/bin/sh\ntouch %s\n" % self.saved)
        os.chmod(j("clock-save.sh"), 0o755)
        with open(j("boot_id"), "w") as f:
            f.write("boot-1\n")
        self.set_leap("Normal")
        with open(self.uptime, "w") as f:
            f.write("100.00 0.00\n")
        self.state, self.dropin, self.installed = j("state"), j("chrony.service.d"), j("opt", "clock-test.sh")
        self.env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], CLOCKTEST_NO_ROOT="1",
                        CLOCKTEST_STATE_DIR=self.state, CLOCKTEST_CLOCK_FILE=self.clock,
                        CLOCKTEST_DROPIN_DIR=self.dropin, CLOCKTEST_INSTALL_PATH=self.installed,
                        CLOCKTEST_CLOCK_SAVE=j("clock-save.sh"), CLOCKTEST_BOOT_ID_FILE=j("boot_id"),
                        CLOCKTEST_UPTIME_FILE=self.uptime, CLOCKTEST_HOLD_STEP_S="0")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def set_leap(self, value):
        with open(self.leap, "w") as f:
            f.write("A,B,C,D,0.000001,F,%s\n" % value)

    def run_script(self, *args, script=SCRIPT):
        return subprocess.run([script, *args], env=self.env, capture_output=True, text=True, timeout=30)

    def calls_log(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def arm(self, *args):
        r = self.run_script("arm", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_arm_sets_the_clock_file_flag_and_dropin(self):
        before = time.time()
        self.arm("--hours-back", "6", "--offline-min", "30")
        self.assertAlmostEqual(os.stat(self.clock).st_mtime, before - 6 * 3600, delta=5)
        with open(os.path.join(self.state, "armed")) as f:
            self.assertIn("OFFLINE_MIN=30", f.read())
        with open(os.path.join(self.dropin, "50-wifi-sensor-clocktest.conf")) as f:
            self.assertIn("ExecStartPost=-%s boot-hook" % self.installed, f.read())
        self.assertTrue(os.access(self.installed, os.X_OK))
        self.assertIn("systemctl daemon-reload", self.calls_log())
        self.assertIn("armed", self.run_script("status").stdout)
        self.assertNotEqual(self.run_script("arm", "--hours-back", "6", "--offline-min", "30").returncode, 0)

    def test_arm_refusals(self):
        for args in (["--hours-back", "6"],                                  # no --offline-min
                     ["--hours-back", "6", "--offline-min", "0"],
                     ["--hours-back", "100", "--offline-min", "30"],
                     ["--hours-back", "6", "--epoch", "2026-09-15T13:52:32Z", "--offline-min", "30"],
                     ["--epoch", "2026-09-15T13:52:32Z", "--offline-min", "30"]):   # weeks back without --lan-only
            r = self.run_script("arm", *args)
            self.assertNotEqual(r.returncode, 0, args)
        self.assertFalse(os.path.exists(os.path.join(self.state, "armed")))
        self.set_leap("Not synchronised")
        self.assertNotEqual(self.run_script("arm", "--hours-back", "6", "--offline-min", "30").returncode, 0)
        self.set_leap("Normal")
        self.arm("--epoch", "2026-09-15T13:52:32Z", "--offline-min", "30", "--lan-only")

    def test_boot_hook_without_flag_does_nothing(self):
        r = self.run_script("boot-hook")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls_log(), [])

    def test_boot_hook_consumes_the_flag_and_starts_the_hold(self):
        self.arm("--hours-back", "6", "--offline-min", "30")
        self.set_leap("Not synchronised")            # the boot: stale clock, no NTP yet
        open(self.calls, "w").close()
        r = self.run_script("boot-hook", script=self.installed)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls_log()
        self.assertIn("chronyc offline", calls)
        self.assertTrue(any(c.startswith("systemd-run --unit=wifi-sensor-clocktest-hold") and c.endswith("hold 30")
                            for c in calls), calls)
        self.assertFalse(os.path.exists(os.path.join(self.state, "armed")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "consumed-boot-1")))
        open(self.calls, "w").close()
        self.run_script("boot-hook", script=self.installed)  # the next boot is a normal one
        self.assertEqual(self.calls_log(), [])

    def test_boot_hook_lost_race_is_a_normal_boot(self):
        self.arm("--hours-back", "6", "--offline-min", "30")
        open(self.calls, "w").close()               # leap still Normal: chrony synced before the hook
        self.run_script("boot-hook", script=self.installed)
        calls = self.calls_log()
        self.assertIn("chronyc online", calls)
        self.assertFalse(any(c.startswith("systemd-run") for c in calls))
        with open(os.path.join(self.state, "log")) as f:
            self.assertIn("lost race", f.read())

    def test_hold_reasserts_offline_then_goes_online(self):
        r = self.run_script("hold", "1")             # each stub 'offline' advances uptime 30 s
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls_log()
        self.assertEqual(calls.count("chronyc offline"), 2)
        self.assertEqual(calls[-1], "chronyc online")

    def test_disarm_removes_flag_and_dropin(self):
        self.arm("--hours-back", "6", "--offline-min", "30")
        r = self.run_script("disarm")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "armed")))
        self.assertFalse(os.path.exists(self.dropin))
        self.assertTrue(os.path.exists(self.saved))
        self.assertIn("chronyc online", self.calls_log())
        self.assertIn("not armed", self.run_script("status").stdout)


if __name__ == "__main__":
    unittest.main()
