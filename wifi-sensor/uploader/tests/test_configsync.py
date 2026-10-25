"""The uploader's part of configuration delivery (uploader/configsync.py,
server/docs/config-delivery.md 3, 4.3, 5.5): a delivered configuration is
written for the applier only when CONFIG_APPLY is on and it is well-formed
and hashes right; config_state and the capabilities are read from the
applier's files for the heartbeat. Standard library only."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sensor"))

import capture_config as cc  # noqa: E402
import configsync as cs  # noqa: E402

CFG = {"channels": ["1", "6", "11"], "dwell_s": 0.2, "poll_s": 30}


def delivered(version=4, cfg=CFG, sha=None, **kw):
    d = {"version": version, "sha256": sha or cc.sha256(cfg), "retry": False, "desired": cfg}
    d.update(kw)
    return d


class Accept(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.desired = os.path.join(self.tmp.name, "config", "desired.json")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        with open(self.desired) as f:
            return json.load(f)

    def test_written_when_accepting(self):
        ok, why = cs.accept_delivery(delivered(), self.desired, accepting=True)
        self.assertTrue(ok, why)
        d = self.read()
        self.assertEqual((d["version"], d["sha256"], d["desired"]), (4, cc.sha256(CFG), CFG))

    def test_never_written_without_config_apply(self):
        ok, why = cs.accept_delivery(delivered(), self.desired, accepting=False)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(self.desired))

    def test_the_same_configuration_is_not_rewritten(self):
        cs.accept_delivery(delivered(), self.desired, accepting=True)
        before = os.stat(self.desired).st_mtime_ns
        ok, why = cs.accept_delivery(delivered(), self.desired, accepting=True)
        self.assertFalse(ok)
        self.assertEqual(why, "already received")
        self.assertEqual(os.stat(self.desired).st_mtime_ns, before)

    def test_malformed_or_wrong_hash_is_ignored(self):
        for bad in (delivered(sha="0" * 64), delivered(version="4"), dict(delivered(), extra=1),
                    {"version": 4, "sha256": cc.sha256(CFG)}, delivered(cfg={"channels": "1,6"}, sha="1" * 64),
                    delivered(cfg=dict(CFG, channels=["1"] * 3000)), "x", None):
            with self.subTest(bad=str(bad)[:60]):
                ok, why = cs.accept_delivery(bad, self.desired, accepting=True)
                self.assertFalse(ok)
                self.assertFalse(os.path.exists(self.desired))


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "applier")
        os.makedirs(self.state_dir)
        self.desired = os.path.join(self.tmp.name, "config", "desired.json")

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, obj):
        with open(os.path.join(self.state_dir, name), "w") as f:
            json.dump(obj, f)

    def test_nothing_yet(self):
        self.assertEqual(cs.config_state(False, self.state_dir, self.desired),
                         {"accepting": False, "applied_version": None, "applied_sha256": None, "attempt": None})

    def test_received_until_the_applier_answers(self):
        cs.accept_delivery(delivered(), self.desired, accepting=True)
        s = cs.config_state(True, self.state_dir, self.desired)
        self.assertEqual((s["attempt"]["version"], s["attempt"]["result"]), (4, "received"))

    def test_the_appliers_result_and_applied(self):
        cs.accept_delivery(delivered(), self.desired, accepting=True)
        self.put("result.json", {"version": 4, "sha256": cc.sha256(CFG), "result": "applied", "reason": None,
                                 "started_poll": 10, "effective_poll": 14, "restored_poll": None,
                                 "checks": {"polls": 4}})
        self.put("applied.json", {"version": 4, "sha256": cc.sha256(CFG)})
        s = cs.config_state(True, self.state_dir, self.desired)
        self.assertEqual((s["applied_version"], s["applied_sha256"]), (4, cc.sha256(CFG)))
        self.assertEqual((s["attempt"]["result"], s["attempt"]["effective_poll"]), ("applied", 14))

    def test_a_result_for_an_older_version_is_superseded_by_a_new_delivery(self):
        self.put("result.json", {"version": 3, "sha256": "a" * 64, "result": "rejected", "reason": "x"})
        cs.accept_delivery(delivered(version=5), self.desired, accepting=True)
        s = cs.config_state(True, self.state_dir, self.desired)
        self.assertEqual((s["attempt"]["version"], s["attempt"]["result"]), (5, "received"))

    def test_unreadable_files_are_none(self):
        with open(os.path.join(self.state_dir, "result.json"), "w") as f:
            f.write("{nope")
        self.assertIsNone(cs.config_state(True, self.state_dir, self.desired)["attempt"])

    def test_capabilities(self):
        self.assertIsNone(cs.capabilities(self.state_dir))
        self.put("capabilities.json", {"channels_supported": ["1", "6"], "reg_country": "SK", "extra": 1})
        self.assertEqual(cs.capabilities(self.state_dir), {"channels_supported": ["1", "6"], "reg_country": "SK"})


if __name__ == "__main__":
    unittest.main()


class InTheUploader(unittest.TestCase):
    """The daemon: the heartbeat carries config_state; a response's config is
    taken only with CONFIG_APPLY=1."""

    def setUp(self):
        sys.path.insert(0, os.path.join(HERE, "..", "..", "collector"))
        sys.path.insert(0, os.path.join(HERE, "..", "..", "collector", "tests"))
        import uploader
        from store import Store
        self.uploader = uploader
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "buffer.db")
        Store(self.db).close()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, apply):
        u = self.uploader
        c = dict(u.DEFAULTS, DB_PATH=self.db, STATE_PATH=os.path.join(self.tmp.name, "state.json"),
                 INGEST_URL="https://ingest.invalid/api/ingest/v1", INGEST_TOKEN="wsi1.7." + "ab" * 32,
                 CAPTURE_IFACE="none0", CONFIG_APPLY=apply, CONFIG_DIR=os.path.join(self.tmp.name, "config"),
                 CONFIG_STATE_DIR=os.path.join(self.tmp.name, "applier"))
        for k in u.NUMERIC:
            c[k] = float(c[k])

        class Fake:
            def post(self, path, payload):
                self.sent = payload
                return 200, {"server_ts": 1.0e9, "resume": None, "config": delivered()}, 20, 0.0
        client = Fake()
        return u.Uploader(c, client, current_boot="bbbbbbbb-0000-4000-8000-000000000009"), client

    def test_taken_with_config_apply(self):
        up, client = self.make("1")
        up.send_heartbeat()
        self.assertTrue(client.sent["heartbeat"]["config_state"]["accepting"])
        self.assertTrue(os.path.exists(up.desired_path))
        self.assertEqual(up.heartbeat()["config_state"]["attempt"]["result"], "received")

    def test_not_taken_without(self):
        up, client = self.make("0")
        up.send_heartbeat()
        self.assertFalse(client.sent["heartbeat"]["config_state"]["accepting"])
        self.assertFalse(os.path.exists(up.desired_path))
