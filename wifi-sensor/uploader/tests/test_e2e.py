"""The uploader against a fake ingest server over real TLS: every poll arrives
exactly once and in order through failures, lost acknowledgements and size
limits; an untrusted certificate and plain http are refused; the token never
appears in the log. Needs the openssl command line (skipped without it).
Standard library only."""

import logging
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector", "tests"))
sys.path.insert(0, HERE)

import fake_server  # noqa: E402
import uploader  # noqa: E402
from client import Client, Refused  # noqa: E402
from shape import shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import AP_RAW, CLIENT_RAW, TS  # noqa: E402

TOKEN = "wsi1.7." + "ab" * 32
BOOT = "bbbbbbbb-0000-4000-8000-000000000002"
HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
LAST = AP_RAW["kismet.device.base.last_time"]


def ap(last):
    raw = dict(AP_RAW)
    raw["kismet.device.base.last_time"] = last
    return raw


@unittest.skipUnless(shutil.which("openssl"), "openssl command line not available")
class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certdir = tempfile.TemporaryDirectory()
        cls.ca, cls.cert, cls.key, cls.self_cert, cls.self_key = fake_server.make_certs(cls.certdir.name)

    @classmethod
    def tearDownClass(cls):
        cls.certdir.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "buffer.db")
        self.store = Store(self.db_path, boot_id=BOOT)
        self.fake = fake_server.Fake(TOKEN)
        self.srv, self.url = fake_server.serve(self.fake, self.cert, self.key)
        uploader.time.sleep = lambda s: None          # no real waiting in the loop

    def tearDown(self):
        self.srv.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def polls(self, n, start=0):
        for i in range(start, start + n):
            ts = TS + 30 * i
            self.store.write_poll(ts, HEALTH, [shape_device(ap(LAST + 30 * i), ts), shape_device(CLIENT_RAW, ts)],
                                  [], 42, clock={"boot_id": BOOT, "mono": 100.0 + 30 * i, "synced": True,
                                                 "chrony_offset": 0.0, "wall": float(ts)})

    def uploader(self, **cfg):
        c = dict(uploader.DEFAULTS, DB_PATH=self.db_path, STATE_PATH=os.path.join(self.tmp.name, "state.json"),
                 INGEST_URL=self.url, INGEST_TOKEN=TOKEN, CAPTURE_IFACE="none0")
        c.update(cfg)
        for k in uploader.NUMERIC:
            c[k] = float(c[k])
        return uploader.Uploader(c, Client(self.url, TOKEN, timeout=5, cafile=self.ca), current_boot=BOOT)

    def run_until_caught_up(self, up, rounds=60):
        for _ in range(rounds):
            up.backoff.until = 0.0
            if up.tick():
                return True
        return False

    def test_every_poll_once_in_order_through_failures(self):
        self.polls(12)
        self.fake.failures = ["status:503", "drop", "status:502", "status:413", "drop"]
        up = self.uploader(CATCHUP_POLLS="5")
        with self.assertLogs("uploader", level="INFO") as logs:
            self.assertTrue(self.run_until_caught_up(up))
        self.assertEqual(sorted(self.fake.polls), list(range(1, 13)))
        self.assertEqual(len(self.fake.obs), 12)                       # AP rows only, each once
        self.assertGreaterEqual(sum(self.fake.uploads.values()), 2)    # the dropped ones came again
        self.assertNotIn("ab" * 32, "\n".join(logs.output))            # never the token
        # more polls later: only those
        self.polls(3, start=12)
        self.assertTrue(self.run_until_caught_up(up))
        self.assertEqual(sorted(self.fake.polls), list(range(1, 16)))
        # the cursor survives a restart of the uploader
        up2 = self.uploader()
        self.assertEqual(up2.state["cursor"]["polls_id"], 15)

    def test_a_new_uploader_starts_after_the_servers_newest_poll(self):
        self.polls(6)
        self.fake.resume = {"seq": 4, "ts": TS + 90}
        self.assertTrue(self.run_until_caught_up(self.uploader()))
        self.assertEqual(sorted(self.fake.polls), [5, 6])

    def test_heartbeat_carries_the_backlog(self):
        self.polls(3)
        up = self.uploader()
        up.ensure_cursor()
        up.send_heartbeat()
        hb = self.fake.heartbeats[-1]
        self.assertEqual(hb["backlog_polls"], 3)
        self.assertEqual(hb["buffer_id"], self.store.get_meta("buffer_id"))
        self.assertIsNone(hb["adapter"])                               # no such interface here

    def test_an_untrusted_certificate_is_refused(self):
        srv, url = fake_server.serve(self.fake, self.self_cert, self.self_key)
        try:
            status, body, _, _ = Client(url, TOKEN, timeout=5).post("/heartbeat", {"v": 1})   # system CAs only
            self.assertEqual(status, 0)
            self.assertIn("CERTIFICATE_VERIFY_FAILED", body["detail"])
            self.assertEqual(self.fake.heartbeats, [])                 # nothing reached the handler
        finally:
            srv.shutdown()

    def test_plain_http_is_refused(self):
        with self.assertRaises(Refused):
            Client("http://localhost:1/api/ingest/v1", TOKEN)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
