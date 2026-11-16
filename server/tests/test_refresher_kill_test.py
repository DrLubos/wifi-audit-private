"""tests/live/refresher_kill_test.sh against a fake `docker`: the script must
kill the refresher only with --run and only while a refresh holds a mark,
start it again whatever happens, and tell a recovered mark (PASS) from a lost
one (FAIL) and from a kill that came too late (nothing tested). Standard
library only; the real run is the developer's, on the box."""

import json
import os
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "live", "refresher_kill_test.sh")
MARK = "2026-11-16 21:10:36+00"

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DOCKER_DIR"]
sc = json.load(open(os.path.join(d, "scenario.json")))
sp = os.path.join(d, "state.json")
st = json.load(open(sp)) if os.path.exists(sp) else {"calls": [], "samples": 0, "killed": False, "started": False, "held": 0}
a = sys.argv[1:]
out = ""
if a[:2] == ["compose", "exec"]:
    sql = a[-1]
    taken_now = ""
    if st["killed"]:
        if sc.get("taken_survives_kill", True) and not (st["started"] and sc.get("restores", True)):
            taken_now = sc["mark"]
    elif st["samples"] > sc.get("taken_after_samples", 1) and st["held"] < sc.get("held_samples", 3):
        taken_now = sc["mark"]
    if "information_schema.columns" in sql:
        out = str(sc.get("column", 1))
    elif "SELECT sensor_id || ' ' || taken_from" in sql:
        st["samples"] += 1
        if st["samples"] > sc.get("taken_after_samples", 1) and sc.get("refresh_comes", True):
            out = "1 " + sc["mark"]
    elif "coalesce(taken_from::text, '')" in sql:
        if not st["killed"]:
            st["held"] += 1
        out = taken_now
    elif "FROM refresh_runs" in sql:
        assert "refresh_from <= '%s'::timestamptz" % sc["mark"] in sql, sql
        assert "started_at >= '20" in sql and "Z'::timestamptz" in sql, sql
        out = "1" if st["started"] and sc.get("restores", True) else "0"
    elif "'dirty_from='" in sql:
        out = "dirty_from=- taken_from=" + (taken_now or "-")
    else:
        sys.exit("fake docker: unexpected SQL: " + sql)
elif a[:3] == ["compose", "ps", "-q"]:
    out = "0123abcd" if sc.get("running", True) else ""
elif a[:2] == ["compose", "kill"]:
    assert a[2:] == ["refresher"], a
    st["calls"].append("kill"); st["killed"] = True
elif a[:3] == ["compose", "up", "-d"]:
    assert a[3:] == ["refresher"], a
    st["calls"].append("up")
    st["started"] = st["killed"]
elif a[:2] == ["compose", "logs"]:
    out = "wifi-audit-refresher-1  | 2026-11-16 21:11:40,000 INFO refresher start: interval=60.0s"
    if st["started"] and sc.get("restores", True):
        out += "\nwifi-audit-refresher-1  | 2026-11-16 21:11:41,000 WARNING sensor_id=1: the mark of a refresh that did not finish is back, from=2026-11-16T21:10:36+00:00"
else:
    sys.exit("fake docker: unexpected call: " + " ".join(a))
json.dump(st, open(sp, "w"))
if out:
    print(out)
'''


class KillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = os.path.join(self.tmp.name, "docker")
        with open(fake, "w") as f:
            f.write(FAKE_DOCKER)
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, args=(), **scenario):
        scenario.setdefault("mark", MARK)
        with open(os.path.join(self.tmp.name, "scenario.json"), "w") as f:
            json.dump(scenario, f)
        env = dict(os.environ, PATH=self.tmp.name + os.pathsep + os.environ["PATH"], FAKE_DOCKER_DIR=self.tmp.name,
                   WAIT_S="1", SLEEP_S="0")
        r = subprocess.run(["sh", SCRIPT, *args], env=env, capture_output=True, text=True, timeout=120)
        calls = []
        sp = os.path.join(self.tmp.name, "state.json")
        if os.path.exists(sp):
            with open(sp) as f:
                calls = json.load(f)["calls"]
        return r.returncode, r.stdout + r.stderr, calls

    def test_a_recovered_mark_passes(self):
        code, out, calls = self.run_script(["--run"])
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[:2], ["kill", "up"], out)
        self.assertEqual(calls.count("kill"), 1)
        for text in ("1/3 the mark stayed in the table after the kill: taken_from=" + MARK,
                     "2/3 the new refresher put the mark back: 2026-11-16 21:11:41,000 WARNING sensor_id=1",
                     "3/3 a rollups step from", "PASS: the refresher killed in the middle of a refresh did not lose"):
            self.assertIn(text, out)

    def test_a_lost_mark_fails_and_the_refresher_is_started_again(self):
        code, out, calls = self.run_script(["--run"], restores=False)
        self.assertEqual(code, 1, out)
        self.assertEqual(calls[:2], ["kill", "up"])
        self.assertIn("2/3 MISSING", out)
        self.assertIn("3/3 MISSING", out)
        self.assertIn("FAIL", out)
        self.assertNotIn("PASS", out)

    def test_a_kill_that_came_too_late_tests_nothing(self):
        code, out, calls = self.run_script(["--run"], taken_survives_kill=False)
        self.assertEqual(code, 2, out)
        self.assertEqual(calls[:2], ["kill", "up"])
        self.assertIn("NOTHING TESTED: the refresh ended before the kill landed", out)
        self.assertNotIn("PASS", out)

    def test_without_run_it_only_watches(self):
        code, out, calls = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, [], out)
        self.assertIn("a refresh holds the mark " + MARK, out)
        self.assertIn("watched only", out)

    def test_no_refresh_in_time_kills_nothing(self):
        code, out, calls = self.run_script(["--run"], refresh_comes=False)
        self.assertEqual(code, 2, out)
        self.assertEqual(calls, [])
        self.assertIn("NOTHING TESTED: no refresh held a mark", out)

    def test_refused_before_schema_21_or_without_a_refresher(self):
        for scenario in ({"column": 0}, {"running": False}):
            code, out, calls = self.run_script(["--run"], **scenario)
            self.assertEqual(code, 3, out)
            self.assertEqual(calls, [], scenario)
        code, out, calls = self.run_script(["--kill"])
        self.assertEqual((code, calls), (3, []), out)

    def test_the_log_line_is_the_refreshers(self):
        with open(os.path.join(HERE, "..", "api", "app", "refresher.py"), encoding="utf-8") as f:
            src = f.read()
        with open(SCRIPT, encoding="utf-8") as f:
            sh = f.read()
        line = "the mark of a refresh that did not finish is back"
        self.assertIn(line, src)
        self.assertIn('BACK="%s"' % line, sh)


if __name__ == "__main__":
    unittest.main()
