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


CHRONY_CONF = """pool 2.debian.pool.ntp.org iburst
server 192.0.2.1 iburst offline   # already offline
sourcedir /run/chrony-dhcp
sourcedir /etc/chrony/sources.d
driftfile /var/lib/chrony/chrony.drift
makestep 1 3
"""

STUB = """#!/bin/sh
echo "$(basename "$0") $*" >> "{calls}"
case "$(basename "$0") $1" in
  "chronyc -c") cat "{leap}" ;;
  "chronyc offline") n=$(cut -d. -f1 "{uptime}"); echo "$((n + 30)).00 0.00" > "{uptime}" ;;
  "chronyc add") [ -e "{fail_add}" ] && exit 1 ;;
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
                f.write(STUB.format(calls=self.calls, leap=self.leap, uptime=self.uptime, fail_add=j("fail-add")))
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
        self.run_dir = j("run")
        os.makedirs(j("etc", "conf.d"))
        self.chrony_conf = j("etc", "chrony.conf")
        with open(self.chrony_conf, "w") as f:
            f.write(CHRONY_CONF)
        with open(j("chronyd"), "w") as f:              # records how it was started
            f.write('#!/bin/sh\necho "chronyd $*" >> "%s"\n' % self.calls)
        os.chmod(j("chronyd"), 0o755)
        self.env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], CLOCKTEST_NO_ROOT="1",
                        CLOCKTEST_STATE_DIR=self.state, CLOCKTEST_CLOCK_FILE=self.clock,
                        CLOCKTEST_DROPIN_DIR=self.dropin, CLOCKTEST_INSTALL_PATH=self.installed,
                        CLOCKTEST_CLOCK_SAVE=j("clock-save.sh"), CLOCKTEST_BOOT_ID_FILE=j("boot_id"),
                        CLOCKTEST_UPTIME_FILE=self.uptime, CLOCKTEST_HOLD_STEP_S="0",
                        CLOCKTEST_RUN_DIR=self.run_dir, CLOCKTEST_CHRONY_CONF=self.chrony_conf,
                        CLOCKTEST_CHRONYD=j("chronyd"))

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
            dropin = f.read()
        self.assertIn("ExecStartPre=-+%s boot-prep" % self.installed, dropin)   # root, no sandbox
        self.assertIn("ExecStart=\nExecStart=!%s chronyd $DAEMON_OPTS" % self.installed, dropin)
        self.assertIn("ExecStartPost=-+%s boot-hook" % self.installed, dropin)
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
        with open(os.path.join(os.path.dirname(self.chrony_conf), "conf.d", "x.conf"), "w") as f:
            f.write("server 192.0.2.9 iburst\n")               # a source the copy would not hold
        self.assertNotEqual(self.run_script("arm", "--hours-back", "6", "--offline-min", "30").returncode, 0)
        os.remove(os.path.join(os.path.dirname(self.chrony_conf), "conf.d", "x.conf"))
        self.arm("--epoch", "2026-09-15T13:52:32Z", "--offline-min", "30", "--lan-only")

    def test_normal_boot_does_nothing(self):
        for step in ("boot-prep", "boot-hook"):
            self.assertEqual(self.run_script(step).returncode, 0)
        self.assertEqual(self.calls_log(), [])
        self.run_script("chronyd", "-F", "1")
        self.assertEqual(self.calls_log(), ["chronyd -F 1"])     # exactly as the unit starts it

    def test_armed_boot_starts_chrony_offline_and_holds(self):
        self.arm("--hours-back", "6", "--offline-min", "30")
        self.set_leap("Not synchronised")            # the boot: stale clock, no NTP yet
        open(self.calls, "w").close()
        r = self.run_script("boot-prep", script=self.installed)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "armed")))
        self.assertTrue(os.path.exists(os.path.join(self.state, "consumed-boot-1")))
        with open(os.path.join(self.run_dir, "chrony.conf")) as f:
            conf = f.read()
        self.assertNotRegex(conf, r"(?m)^\s*(pool|server|peer)\s")             # no time source at all
        self.assertNotRegex(conf, r"(?m)^sourcedir")
        with open(os.path.join(self.run_dir, "sources")) as f:
            self.assertEqual(f.read().splitlines(), ["pool 2.debian.pool.ntp.org iburst",
                                                     "server 192.0.2.1 iburst offline"])   # comment stripped
        self.assertIn("makestep 1 3", conf)
        self.run_script("chronyd", "-F", "1", script=self.installed)
        self.assertIn("chronyd -F 1 -f %s" % os.path.join(self.run_dir, "chrony.conf"), self.calls_log())
        r = self.run_script("boot-hook", script=self.installed)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(c.startswith("systemd-run --unit=wifi-sensor-clocktest-hold") and c.endswith("hold 30")
                            for c in self.calls_log()), self.calls_log())
        open(self.calls, "w").close()
        self.run_script("boot-hook", script=self.installed)     # chrony restarted in the same boot
        self.assertEqual(self.calls_log(), [])
        # the next boot: /run is empty again
        shutil.rmtree(self.run_dir)
        for step in ("boot-prep", "boot-hook"):
            self.run_script(step, script=self.installed)
        self.run_script("chronyd", "-F", "1", script=self.installed)
        self.assertEqual(self.calls_log(), ["chronyd -F 1"])

    def test_boot_prep_without_write_access_stays_armed(self):
        self.arm("--hours-back", "6", "--offline-min", "30")
        os.chmod(self.state, 0o555)                 # as inside chrony's sandbox: /var read-only
        try:
            r = self.run_script("boot-prep", script=self.installed)
        finally:
            os.chmod(self.state, 0o755)
        self.assertEqual(r.returncode, 0)
        self.assertIn("cannot consume the arm flag", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "chrony.conf")))   # chrony starts normally
        self.assertTrue(os.path.exists(os.path.join(self.state, "armed")))

    def test_synchronised_anyway_is_a_normal_boot(self):
        self.arm("--hours-back", "6", "--offline-min", "30")
        self.run_script("boot-prep", script=self.installed)
        open(self.calls, "w").close()               # leap still Normal
        self.run_script("boot-hook", script=self.installed)
        calls = self.calls_log()
        self.assertIn("chronyc online", calls)
        self.assertFalse(any(c.startswith("systemd-run") for c in calls))
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "chrony.conf")))

    def hold_setup(self):
        os.makedirs(self.run_dir)
        open(os.path.join(self.run_dir, "chrony.conf"), "w").close()
        with open(os.path.join(self.run_dir, "sources"), "w") as f:
            f.write("pool 2.debian.pool.ntp.org iburst\nserver 192.0.2.1 iburst\n")

    def test_hold_adds_the_sources_and_drops_the_copy(self):
        self.hold_setup()
        r = self.run_script("hold", "1")             # each stub 'offline' advances uptime 30 s
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls_log()
        self.assertEqual(calls.count("chronyc offline"), 2)
        self.assertEqual(calls[-3:], ["chronyc add pool 2.debian.pool.ntp.org iburst",
                                      "chronyc add server 192.0.2.1 iburst", "chronyc online"])
        self.assertNotIn("systemctl restart chrony", calls)
        for f in ("chrony.conf", "sources"):
            self.assertFalse(os.path.exists(os.path.join(self.run_dir, f)), f)

    def test_failed_add_restarts_chrony_on_the_normal_config(self):
        self.hold_setup()
        open(os.path.join(self.tmp, "fail-add"), "w").close()
        r = self.run_script("hold", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls_log()[-1], "systemctl restart chrony")
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "chrony.conf")))   # restart = normal config

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
