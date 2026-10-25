"""The configuration applier (sensor/config_apply.py, server/docs/config-delivery.md
5): validation of the untrusted desired.json, the rewrite of the two files,
apply -> verify -> applied, rollback when capture does not verify, failed
when the rollback does not verify either, recovery after a reboot mid-apply,
no re-apply of a refused hash, capabilities. A fake Kismet and a fake
systemctl; the buffer is a real collector buffer the fake "collector" writes
polls into while the applier waits. Standard library only."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector"))

import capture_config as cc  # noqa: E402
import config_apply as ca  # noqa: E402
from store import Store  # noqa: E402

ALL = ["1", "6", "11"]            # 3 entries: stride falls back to 1, every entry visited
NEW = ["1", "6", "11", "36", "40"]
SITE = ('# kismet_site.conf\nlog_types=kismet\nsource=wlan1:name=capture,type=linuxwifi,channels="%s"\n'
        'alertbacklog=50\n' % ",".join(ALL))
COLL = "# collector\nPOLL_INTERVAL=30\nPOLL_OVERLAP=5\n"
SUPPORTED = ALL + ["36", "40", "44", "165HT40-"]
T0 = 1_792_900_000


class FakeKismet:
    def __init__(self):
        self.live = list(ALL)
        self.rate = 5.0
        self.running = True
        self.calls = []
        self.accept = True            # set_hop takes effect

    def sources(self):
        return [{"kismet.datasource.uuid": "U1", "kismet.datasource.name": "capture",
                 "kismet.datasource.running": self.running, "kismet.datasource.hop_channels": list(self.live),
                 "kismet.datasource.hop_rate": self.rate, "kismet.datasource.hop_shuffle": 1,
                 "kismet.datasource.channels": list(SUPPORTED)}]

    def set_hop(self, uuid, channels, rate, shuffle):
        self.calls.append((channels, rate))
        if self.accept:
            self.live, self.rate = channels.split(","), float(rate)


class Rig(unittest.TestCase):
    """A temp /etc + state dir + buffer; sleep() is the collector writing a poll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.cfg = dict(ca.DEFAULTS, DESIRED=os.path.join(t, "up", "desired.json"), STATE_DIR=os.path.join(t, "st"),
                        KISMET_SITE_CONF=os.path.join(t, "kismet_site.conf"),
                        COLLECTOR_CONF=os.path.join(t, "collector.conf"), DB_PATH=os.path.join(t, "buffer.db"))
        for k in ca.NUMERIC:
            self.cfg[k] = float(self.cfg[k])
        os.makedirs(self.cfg["STATE_DIR"])
        os.makedirs(os.path.dirname(self.cfg["DESIRED"]))
        with open(self.cfg["KISMET_SITE_CONF"], "w") as f:
            f.write(SITE)
        with open(self.cfg["COLLECTOR_CONF"], "w") as f:
            f.write(COLL)
        self.store = Store(self.cfg["DB_PATH"])
        self.clock = T0
        self.packets = 1000
        self.kismet = FakeKismet()
        self.restarts = []
        self.capture_ok = True        # the fake collector sees the configured list
        self.broken = None            # a hop list that never collects
        self.poll_s = 30
        for _ in range(3):
            self.poll()
        self.applier = ca.Applier(self.cfg, self.kismet, systemctl=lambda *a: self.restarts.append(a),
                                  sleep=self.sleep, now=lambda: self.clock)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def poll(self):
        """One collector poll: the hop list as Kismet has it, hop_ok against the file."""
        with open(self.cfg["KISMET_SITE_CONF"]) as f:
            configured, _ = ca.site_conf_config(f.read(), "capture")
        live = self.kismet.live
        ok = int(self.capture_ok and ca.same_list(live, configured) and self.kismet.running
                 and not (self.broken and ca.same_list(live, self.broken)))
        self.packets += 500
        self.store.write_poll(int(self.clock), {"kismet_ts": int(self.clock), "devices_total": 50, "ds_running": 1,
                                                "ds_error": None, "ds_packets": self.packets, "ds_hop_n": len(live),
                                                "ds_hop_visited": len(live), "ds_hop_ok": ok}, [], [], 5)
        # write_poll stores devices_active = len(records): give the poll devices
        self.store.db.execute("UPDATE polls SET devices_active = 40 WHERE id = (SELECT max(id) FROM polls)")

    def sleep(self, s):
        self.clock += s
        if int(self.clock) % self.poll_s < s:
            self.poll()

    def deliver(self, channels=NEW, dwell=0.2, poll=30, version=4, retry=False, sha=None):
        cfg = {"channels": channels, "dwell_s": dwell, "poll_s": poll}
        with open(self.cfg["DESIRED"], "w") as f:
            json.dump({"version": version, "sha256": sha or cc.sha256(cfg), "retry": retry, "desired": cfg}, f)
        return cfg

    def state(self, name):
        p = os.path.join(self.cfg["STATE_DIR"], name)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def site(self):
        with open(self.cfg["KISMET_SITE_CONF"]) as f:
            return f.read()


class Files(unittest.TestCase):
    def test_source_line_rewrite_keeps_everything_else(self):
        out = ca.site_conf_with(SITE, "capture", NEW, 0.5)
        self.assertIn('source=wlan1:name=capture,type=linuxwifi,channels="1,6,11,36,40",channel_hoprate=2/sec', out)
        self.assertEqual([x for x in out.split("\n") if not x.startswith("source=")],
                         [x for x in SITE.split("\n") if not x.startswith("source=")])
        self.assertEqual(ca.site_conf_config(out, "capture"), (NEW, 0.5))
        self.assertEqual(ca.site_conf_config(SITE, "capture"), (ALL, 0.2))   # no hoprate: Kismet's 5/sec
        again = ca.site_conf_with(out, "capture", ALL, 0.2)
        self.assertEqual(again.count("channel_hoprate"), 1)

    def test_commented_source_lines_are_left_alone(self):
        site = "#source=wlan9:name=old\n" + SITE
        out = ca.site_conf_with(site, "capture", NEW, 0.2)
        self.assertTrue(out.startswith("#source=wlan9:name=old\n"))

    def test_collector_poll_interval(self):
        self.assertEqual(ca.collector_poll_s(ca.collector_conf_with(COLL, 60)), 60)
        self.assertEqual(ca.collector_conf_with(COLL, 60).count("POLL_INTERVAL"), 1)
        self.assertEqual(ca.collector_poll_s(ca.collector_conf_with("X=1\n", 15)), 15)
        self.assertEqual(ca.hop_rate(0.2), 5)
        self.assertEqual(ca.hop_rate(1.0), 1)


class Apply(Rig):
    def test_applied(self):
        self.deliver()
        r = self.applier.apply()
        self.assertEqual(r["result"], "applied", r)
        self.assertEqual(self.kismet.live, NEW)
        self.assertIn('channels="1,6,11,36,40"', self.site())
        self.assertEqual(self.state("applied.json")["version"], 4)
        self.assertEqual(self.state("applied.json")["effective_poll"], r["effective_poll"])
        self.assertGreater(r["effective_poll"], r["started_poll"])
        self.assertIsNone(self.state("attempt.json"))
        self.assertEqual(self.restarts, [])                       # same poll interval: no collector restart
        # the new files are the last good ones now
        with open(os.path.join(self.cfg["STATE_DIR"], "last-good", "kismet_site.conf")) as f:
            self.assertIn("36,40", f.read())

    def test_poll_interval_restarts_the_collector(self):
        self.deliver(channels=ALL, poll=60)
        self.poll_s = 60                                          # the restarted collector polls every 60 s
        r = self.applier.apply()
        self.assertEqual(r["result"], "applied", r)
        self.assertEqual(self.restarts, [("restart", "wifi-sensor-collector.service")])
        with open(self.cfg["COLLECTOR_CONF"]) as f:
            self.assertIn("POLL_INTERVAL=60", f.read())

    def test_running_configuration_is_not_reapplied(self):
        self.deliver()
        self.applier.apply()
        n = len(self.kismet.calls)
        self.assertIsNone(self.applier.apply())
        self.assertEqual(len(self.kismet.calls), n)


class Refused(Rig):
    def test_rejected_before_anything_changes(self):
        for kw, why in (({"channels": ["1", "6", "14"]}, "not supported"),
                        ({"channels": ["165HT40-"]}, "40 MHz pairing"),
                        ({"sha": "0" * 64}, "sha256")):
            with self.subTest(why):
                self.deliver(**kw)
                r = self.applier.apply()
                self.assertEqual(r["result"], "rejected")
                self.assertIn(why, r["reason"])
                self.assertEqual(self.site(), SITE)
                self.assertEqual(self.kismet.calls, [])

    def test_malformed_or_oversized_desired(self):
        with open(self.cfg["DESIRED"], "w") as f:
            f.write(json.dumps({"version": "4", "sha256": "x", "desired": {}}))
        self.assertEqual(self.applier.apply()["result"], "rejected")
        with open(self.cfg["DESIRED"], "w") as f:
            f.write(" " * 9000)
        self.assertIsNone(self.applier.apply())                   # unreadable: nothing to report

    def test_capture_not_verifying_rolls_back(self):
        self.deliver()
        self.broken = NEW                                         # the new list never collects
        r = self.applier.apply()
        self.assertEqual(r["result"], "rolled_back", r)
        self.assertIn("capture did not verify", r["reason"])
        self.assertEqual(self.site(), SITE)                       # the last good file is back
        self.assertEqual(self.kismet.live, ALL)
        self.assertIsNone(self.state("attempt.json"))
        self.assertIsNone(self.state("applied.json"))
        self.assertIsNotNone(r["restored_poll"])

    def test_capture_did_verify_after_restore(self):
        # the restored configuration collects again
        self.deliver()
        calls = {"n": 0}
        orig = self.kismet.set_hop

        def set_hop(uuid, channels, rate, shuffle):
            calls["n"] += 1
            self.capture_ok = calls["n"] > 1                      # the first (new) list fails, the restored works
            orig(uuid, channels, rate, shuffle)
        self.kismet.set_hop = set_hop
        r = self.applier.apply()
        self.assertEqual(r["result"], "rolled_back")
        self.assertTrue(r["checks"]["rollback"]["live_list_ok"])

    def test_failed_when_the_rollback_does_not_verify(self):
        self.deliver()
        self.capture_ok = False
        self.kismet.accept = False                                # Kismet ignores every set_channel
        r = self.applier.apply()
        self.assertEqual(r["result"], "failed")
        self.assertIn("rollback did not verify", r["reason"])
        self.assertEqual(self.site(), SITE)                       # the last good files stay in place

    def test_a_refused_hash_is_not_applied_again_without_retry(self):
        self.deliver()
        self.broken = NEW
        self.assertEqual(self.applier.apply()["result"], "rolled_back")
        self.broken = None
        n = len(self.kismet.calls)
        self.assertIsNone(self.applier.apply())                   # the same hash, no retry
        self.assertEqual(len(self.kismet.calls), n)
        self.deliver(retry=True)
        self.assertEqual(self.applier.apply()["result"], "applied")

    def test_source_not_running(self):
        self.deliver()
        self.kismet.running = False
        r = self.applier.apply()
        self.assertEqual(r["result"], "rejected")
        self.assertIn("not running", r["reason"])


class Recover(Rig):
    def test_an_interrupted_apply_is_undone_at_boot(self):
        self.deliver()
        self.applier.keep_last_good(SITE, COLL)
        ca.write_json(os.path.join(self.cfg["STATE_DIR"], "attempt.json"),
                      {"version": 4, "sha256": "a" * 64, "phase": "apply", "started_poll": 3})
        with open(self.cfg["KISMET_SITE_CONF"], "w") as f:
            f.write(ca.site_conf_with(SITE, "capture", NEW, 0.2))   # the half-applied state
        r = self.applier.recover()
        self.assertEqual(r["result"], "rolled_back")
        self.assertEqual(self.site(), SITE)
        self.assertIsNone(self.state("attempt.json"))
        self.assertIsNone(self.applier.recover())                  # nothing left to do

    def test_nothing_to_recover(self):
        self.assertIsNone(self.applier.recover())


class Capabilities(Rig):
    def test_reported(self):
        c = self.applier.capabilities()
        self.assertEqual(c["channels_supported"], SUPPORTED)
        self.assertIn("reg_country", c)


if __name__ == "__main__":
    unittest.main()
