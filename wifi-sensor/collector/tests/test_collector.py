"""collector.poll_once() against a fake Kismet: whatever shape one record,
one alert or a health field arrives in, the poll is stored and the resume
point moves on (the crash loop of 2026-11-12: one value SQLite could not
bind stopped collection for 56 minutes). Standard library only - `requests`
is stubbed, the fake client never touches the network."""

import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if "requests" not in sys.modules:                 # kismet_client imports it; not installed on the dev machine
    try:
        import requests  # noqa: F401
    except ImportError:
        stub = types.ModuleType("requests")
        stub.Session = object
        stub.RequestException = Exception
        sys.modules["requests"] = stub

import collector  # noqa: E402
from kismet_client import KismetError  # noqa: E402
from store import Store  # noqa: E402
from test_shape import ALERT_RAW, AP_RAW, CLIENT_RAW, TS  # noqa: E402

CFG = {"POLL_OVERLAP": 5, "CONFIG_APPLIED": None}
SOURCE = {"kismet.datasource.name": "capture", "kismet.datasource.running": 1, "kismet.datasource.num_packets": 10,
          "kismet.datasource.hop_channels": ["1", "6"], "kismet.datasource.hop_shuffle": 0}


class FakeKismet:
    def __init__(self, status=None, sources=None, devices=None, alerts=None, fail=None):
        self._status = {"kismet.system.timestamp.sec": TS + 1, "kismet.system.devices.count": 2} \
            if status is None else status
        self._sources = [SOURCE] if sources is None else sources
        self._devices = [AP_RAW, CLIENT_RAW] if devices is None else devices
        self._alerts = [] if alerts is None else alerts
        self._fail = fail

    def status(self):
        if self._fail:
            raise KismetError(self._fail)
        return self._status

    def datasources(self):
        return self._sources

    def devices_since(self, since, fields):
        return self._devices

    def alerts_since(self, since):
        return self._alerts


class PollOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "buffer.db"))
        self.clk = {"boot_id": None, "wall": float(TS), "mono": 100.0, "synced": True, "chrony_offset": 0.0}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql):
        return self.store.db.execute(sql).fetchall()

    def poll(self, client, since=0):
        return collector.poll_once(client, self.store, CFG, since, None, self.clk)

    def test_a_plain_poll(self):
        self.assertEqual(self.poll(FakeKismet()), TS + 1)
        self.assertEqual(self.q("SELECT ok, new_obs, devices_total, ds_running, ds_packets FROM polls"),
                         [(1, 2, 2, 1, 10)])

    def test_records_in_an_unexpected_shape_are_skipped(self):
        devices = [AP_RAW, 7, "text", None, ["a list"], {"kismet.device.base.key": "K", "kismet.device.base.macaddr": 5},
                   CLIENT_RAW]
        self.assertEqual(self.poll(FakeKismet(devices=devices)), TS + 1)
        self.assertEqual(self.q("SELECT ok, new_obs, devices_active FROM polls"), [(1, 2, 2)])

    def test_a_value_no_column_can_hold_does_not_stop_the_poll(self):
        huge = dict(AP_RAW)
        huge["kismet.device.base.key"] = "4202770D00000000_0000000000AA"
        huge["kismet.device.base.macaddr"] = "AA:00:00:00:00:00"
        huge["kismet.device.base.packets.total"] = 2 ** 80          # shape maps it to unknown
        self.assertEqual(self.poll(FakeKismet(devices=[huge, CLIENT_RAW])), TS + 1)
        self.assertEqual(self.q("SELECT ok, new_obs FROM polls"), [(1, 2)])

    def test_an_alert_that_cannot_be_shaped_is_skipped(self):
        class Unhashable(dict):
            def get(self, key, default=None):
                raise RuntimeError("no such alert field")
        with self.assertLogs("collector", level="WARNING"):
            self.poll(FakeKismet(alerts=[Unhashable(), 5, ALERT_RAW]))
        self.assertEqual(self.q("SELECT count(*) FROM alerts"), [(1,)])
        self.assertEqual(self.q("SELECT ok FROM polls"), [(1,)])

    def test_a_status_that_is_not_an_object_is_a_failed_poll_not_a_crash(self):
        self.assertEqual(self.poll(FakeKismet(status=[1, 2]), since=77), 77)
        self.assertEqual(self.q("SELECT ok, error FROM polls"), [(0, "GET /system/status.json: not a JSON object")])

    def test_health_fields_in_any_shape(self):
        sources = [dict(SOURCE, **{"kismet.datasource.num_packets": "many", "kismet.datasource.hop_channels": 5}), "x"]
        status = {"kismet.system.timestamp.sec": "now", "kismet.system.devices.count": {"n": 1}}
        with self.assertLogs("collector", level="WARNING"):
            self.assertEqual(self.poll(FakeKismet(status=status, sources=sources)), TS)   # falls back to the poll ts
        self.assertEqual(self.q("SELECT ok, kismet_ts, devices_total, ds_packets, ds_hop_n FROM polls"),
                         [(1, TS, None, 0, None)])

    def test_a_long_datasource_error_is_cut_to_the_servers_limit(self):
        src = dict(SOURCE, **{"kismet.datasource.running": 0, "kismet.datasource.error": 1,
                              "kismet.datasource.error_reason": "x" * 2000})
        self.poll(FakeKismet(sources=[src]))
        err = self.q("SELECT ds_error FROM polls")[0][0]
        self.assertEqual(len(err), collector.DS_ERROR_MAX)
        self.assertTrue(err.startswith("capture: xxx"))

    def test_kismet_unreachable(self):
        self.assertEqual(self.poll(FakeKismet(fail="GET /system/status.json: refused"), since=5), 5)
        self.assertEqual(self.q("SELECT ok, error FROM polls"), [(0, "GET /system/status.json: refused")])


class Text(unittest.TestCase):
    def test_nul_characters_never_reach_the_buffer(self):
        from shape import shape_alert, shape_device
        raw = dict(AP_RAW, ssid="cafe\x00wifi", country="S\x00K")
        rec = shape_device(raw, TS)
        self.assertEqual((rec["ap"]["ssid"], rec["ap"]["country"]), ("cafewifi", "SK"))
        alert = shape_alert(dict(ALERT_RAW, **{"kismet.alert.text": "flood on \x00 net"}))
        self.assertEqual(alert["text"], "flood on  net")
        self.assertNotIn("\x00", alert["raw"])                  # json.dumps escapes it; the audit copy stays complete
        self.assertIn("\\u0000", alert["raw"])


if __name__ == "__main__":
    unittest.main()
