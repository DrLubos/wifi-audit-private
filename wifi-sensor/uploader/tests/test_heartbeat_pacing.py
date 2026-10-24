"""Heartbeat pacing without contact (found in the real outage test of
2026-10-20..23, findings.md section 6): with the network down the uploader
must not retry its heartbeat on every 2-s tick (it did - 116,815 refused
heartbeats and journal lines in 3 days). A failed heartbeat backs off to
about HEARTBEAT_S; contact is still found within about a minute once the
network is back. Standard library only, no network."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "collector", "tests"))

import uploader  # noqa: E402
from shape import shape_device  # noqa: E402
from store import Store  # noqa: E402
from test_shape import AP_RAW, TS  # noqa: E402

BOOT = "bbbbbbbb-0000-4000-8000-000000000003"
HEALTH = {"kismet_ts": TS, "devices_total": 1, "ds_running": 1, "ds_error": None, "ds_packets": 1000}


class FakeClient:
    """Answers like client.Client.post: (status, body, rtt_ms, mono_mid)."""

    def __init__(self, clock):
        self.clock = clock
        self.up = False
        self.calls = []

    def post(self, path, payload):
        self.calls.append((self.clock.t, path))
        if not self.up:
            return 0, {"detail": "gaierror: [Errno -3] Temporary failure in name resolution"}, None, self.clock.t
        if path == "/heartbeat":
            return 200, {"server_ts": TS + self.clock.t, "resume": None, "last_batch_seq": None}, 20, self.clock.t
        return 200, {"inserted": {}}, 20, self.clock.t

    def heartbeats(self, since=0.0):
        return [t for t, p in self.calls if p == "/heartbeat" and t >= since]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


class HeartbeatPacing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "buffer.db")
        self.store = Store(self.db_path, boot_id=BOOT)
        # one unsynchronised poll of this boot: held back, so no batch is due -
        # the case of the outage test (a boot without network and without NTP)
        self.store.write_poll(TS, HEALTH, [shape_device(AP_RAW, TS)], [], 42,
                              clock={"boot_id": BOOT, "mono": 100.0, "synced": False,
                                     "chrony_offset": None, "wall": float(TS)})
        self.clock = Clock()
        self._mono, self._sleep = uploader.time.monotonic, uploader.time.sleep
        uploader.time.monotonic = self.clock.monotonic
        uploader.time.sleep = lambda s: None
        self.client = FakeClient(self.clock)

    def tearDown(self):
        uploader.time.monotonic, uploader.time.sleep = self._mono, self._sleep
        self.store.close()
        self.tmp.cleanup()

    def uploader(self, with_cursor=True):
        c = dict(uploader.DEFAULTS, DB_PATH=self.db_path, STATE_PATH=os.path.join(self.tmp.name, "state.json"),
                 INGEST_URL="https://ingest.invalid/api/ingest/v1", INGEST_TOKEN="wsi1.7." + "ab" * 32,
                 CAPTURE_IFACE="none0")
        for k in uploader.NUMERIC:
            c[k] = float(c[k])
        up = uploader.Uploader(c, self.client, current_boot=BOOT)
        if with_cursor:
            bid = self.store.db.execute("SELECT value FROM meta WHERE key = 'buffer_id'").fetchone()[0]
            up.state = {"cursor": {"buffer_id": bid, "polls_id": 0, "obs_id": 0, "dch_id": 0, "alerts_id": 0,
                                   "load_ts": 0, "freq_ts": 0, "batch_seq": 0},
                        "server_anchors": {}, "unverified": {}, "corrected": {}}
        return up

    def advance(self, up, seconds):
        end = self.clock.t + seconds
        while self.clock.t < end:
            up.tick()
            self.clock.t += float(up.cfg["WATCH_S"])

    def test_no_contact_does_not_send_a_heartbeat_every_tick(self):
        up = self.uploader()
        self.advance(up, 3600)                              # one hour without network, 1800 ticks
        n = len(self.client.heartbeats())
        self.assertLess(n, 75, "%d heartbeats in an hour without contact" % n)
        self.assertGreater(n, 50)                       # still about one a minute

    def test_no_cursor_yet_is_paced_too(self):
        up = self.uploader(with_cursor=False)           # first start (or new buffer) without network
        self.advance(up, 3600)
        self.assertLess(len(self.client.heartbeats()), 75)

    def test_contact_is_found_within_about_a_minute_after_the_network_returns(self):
        up = self.uploader()
        self.advance(up, 3 * 3600)
        back = self.clock.t
        self.client.up = True
        self.advance(up, 120)
        first = self.client.heartbeats(since=back)
        self.assertTrue(first)
        self.assertLessEqual(first[0] - back, 1.2 * float(up.cfg["HEARTBEAT_S"]) + float(up.cfg["WATCH_S"]))

    def test_with_contact_one_heartbeat_per_heartbeat_s(self):
        self.client.up = True
        up = self.uploader()
        self.advance(up, 600)
        n = len(self.client.heartbeats())
        self.assertGreaterEqual(n, 9)
        self.assertLessEqual(n, 11)

    def test_a_short_blip_is_retried_quickly(self):
        up = self.uploader()
        self.advance(up, 4)                                 # first try fails
        self.client.up = True
        back = self.clock.t
        self.advance(up, 30)
        first = self.client.heartbeats(since=back)
        self.assertTrue(first)
        self.assertLessEqual(first[0] - back, 8.0)       # the first retry is ~5 s, not a minute


if __name__ == "__main__":
    unittest.main()
