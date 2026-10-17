"""Buffer schema v8 (live upload, server/docs/live-upload.md 4.1 and 8.3): the
clock data per poll, one anchor per boot, rows linked to their poll, the
"new observation" rule per boot, the resume point per boot, the AP load of
unsynchronised polls held until the anchor, pruning by corrected time and
append order, the buffer id. Standard library only."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shape import shape_alert, shape_device  # noqa: E402
from store import SCHEMA_VERSION, Store  # noqa: E402
from test_shape import ALERT_RAW, AP_RAW, CLIENT_RAW, TS  # noqa: E402

HEALTH = {"kismet_ts": TS, "devices_total": 2, "ds_running": 1, "ds_error": None, "ds_packets": 1000}
BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"
DAY = 86400


def clock(boot, mono, synced, wall=None, offset=0.0):
    return {"boot_id": boot, "mono": mono, "synced": synced, "chrony_offset": offset, "wall": wall}


def ap_raw(last, **kw):
    raw = dict(AP_RAW, **kw)
    raw["kismet.device.base.last_time"] = last
    return raw


class V8(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "buffer.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql, *args):
        return self.store.db.execute(sql, args).fetchall()

    def poll(self, ts, raws, clk, alerts=()):
        recs = [shape_device(r, ts) for r in raws]
        return self.store.write_poll(ts, HEALTH, recs, list(alerts), 42, clock=clk)

    def test_version_and_buffer_id(self):
        self.assertEqual(SCHEMA_VERSION, 8)
        bid = self.store.get_meta("buffer_id")
        self.assertRegex(bid, r"^[0-9a-f-]{36}$")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_meta("buffer_id"), bid)       # written once

    def test_poll_carries_the_clock_and_rows_their_poll(self):
        self.poll(TS, [AP_RAW, CLIENT_RAW], clock(BOOT_A, 100.5, True, wall=TS + 0.25, offset=0.001),
                  alerts=[shape_alert(ALERT_RAW)])
        pid, mono, boot, synced, off = self.q("SELECT id, mono, boot_id, clock_synced, chrony_offset FROM polls")[0]
        self.assertEqual((mono, boot, synced, off), (100.5, BOOT_A, 1, 0.001))
        self.assertEqual({r[0] for r in self.q("SELECT poll_id FROM observations")}, {pid})
        self.assertEqual(self.q("SELECT poll_id FROM alerts"), [(pid,)])
        self.assertEqual(self.q("SELECT new_obs FROM polls"), [(2,)])

    def test_history_row_carries_its_poll(self):
        self.poll(TS, [AP_RAW], clock(BOOT_A, 100.0, True, wall=TS))
        changed = dict(ap_raw(AP_RAW["kismet.device.base.last_time"] + 30), adv_ch="11")
        self.poll(TS + 30, [changed], clock(BOOT_A, 130.0, True, wall=TS + 30))
        rows = self.q("SELECT h.poll_id, p.ts FROM device_config_history h JOIN polls p ON p.id = h.poll_id")
        self.assertEqual([ts for _, ts in rows], [TS + 30])

    def test_one_anchor_per_boot_at_the_first_synced_poll(self):
        self.poll(TS, [AP_RAW], clock(BOOT_A, 10.0, False))
        self.assertEqual(self.q("SELECT COUNT(*) FROM clock_anchors"), [(0,)])
        self.poll(TS + 30, [], clock(BOOT_A, 40.0, True, wall=TS + 30.4))
        self.poll(TS + 60, [], clock(BOOT_A, 70.0, True, wall=TS + 60.4))
        self.assertEqual(self.q("SELECT boot_id, mono, wall, source FROM clock_anchors"),
                         [(BOOT_A, 40.0, TS + 30.4, "ntp")])
        self.poll(TS + 90, [], clock(BOOT_B, 5.0, True, wall=TS + 90.1))
        self.assertEqual(self.q("SELECT COUNT(*) FROM clock_anchors"), [(2,)])

    def test_resume_point_is_reset_by_a_new_boot(self):
        self.store.set_meta("last_kismet_ts", TS)
        self.assertTrue(self.store.boot_started(BOOT_A))       # the first boot this buffer sees
        self.assertIsNone(self.store.get_meta("last_kismet_ts"))
        self.store.set_meta("last_kismet_ts", TS)
        self.assertFalse(self.store.boot_started(BOOT_A))      # a collector restart in the same boot
        self.assertEqual(self.store.get_meta("last_kismet_ts"), str(TS))
        self.assertTrue(self.store.boot_started(BOOT_B))
        self.assertIsNone(self.store.get_meta("last_kismet_ts"))

    def test_new_observation_per_boot_under_a_stale_clock(self):
        last = AP_RAW["kismet.device.base.last_time"]
        self.poll(TS, [ap_raw(last)], clock(BOOT_A, 100.0, True, wall=TS))
        # boot B on a clock 30 days behind: Kismet's last_time is far below the stored last_seen
        stale = last - 30 * DAY
        self.assertEqual(self.poll(TS - 30 * DAY, [ap_raw(stale)], clock(BOOT_B, 20.0, False))[0], 1)
        self.assertEqual(self.poll(TS - 30 * DAY + 30, [ap_raw(stale)], clock(BOOT_B, 50.0, False))[0], 0)
        self.assertEqual(self.poll(TS - 30 * DAY + 60, [ap_raw(stale + 40)], clock(BOOT_B, 80.0, False))[0], 1)
        # the device's merged last_seen is not moved back by the stale value
        self.assertEqual(self.q("SELECT last_seen FROM devices"), [(last,)])

    def test_load_of_unsynced_polls_waits_for_the_anchor(self):
        last = AP_RAW["kismet.device.base.last_time"]
        stale_ts = TS - 30 * DAY                    # what the stale clock says
        self.poll(stale_ts, [ap_raw(last - 30 * DAY)], clock(BOOT_B, 20.0, False))
        self.assertEqual(self.q("SELECT COUNT(*) FROM ap_load_hourly"), [(0,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM ap_load_pending"), [(1,)])
        # chrony steps; the anchor: wall TS + 3600.6 at mono 3620.0 -> epoch TS - 19.4 + ...
        anchor_wall = TS + 3600.6
        self.poll(TS + 3600, [ap_raw(last + 3600)], clock(BOOT_B, 3620.0, True, wall=anchor_wall))
        self.assertEqual(self.q("SELECT COUNT(*) FROM ap_load_pending"), [(0,)])
        epoch = anchor_wall - 3620.0
        true_ts = round(epoch + 20.0)
        hours = sorted(r[0] for r in self.q("SELECT hour FROM ap_load_hourly"))
        self.assertIn(true_ts - true_ts % 3600, hours)          # the pending poll, on its true hour
        self.assertNotIn(stale_ts - stale_ts % 3600, hours)     # never on the stale one

    def test_prune_keeps_a_stale_stretch_until_its_true_time_is_old(self):
        last = AP_RAW["kismet.device.base.last_time"]
        now = TS + 3600
        # boot B: one unsynced poll stamped 30 days back, then the anchor
        self.poll(TS - 30 * DAY, [ap_raw(last - 30 * DAY)], clock(BOOT_B, 20.0, False))
        self.poll(now, [ap_raw(last + 3600)], clock(BOOT_B, 3620.0, True, wall=now + 0.6))
        self.store.prune(now - 14 * DAY)                         # the stale ts is older than the cutoff
        self.assertEqual(self.q("SELECT COUNT(*) FROM polls"), [(2,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM observations"), [(2,)])
        self.store.prune(now + 14 * DAY)                         # now both are old by their true time
        self.assertEqual(self.q("SELECT COUNT(*) FROM polls"), [(0,)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM observations"), [(0,)])

    def test_prune_of_a_boot_that_never_got_an_anchor(self):
        last = AP_RAW["kismet.device.base.last_time"]
        self.poll(TS - 30 * DAY, [ap_raw(last - 30 * DAY)], clock(BOOT_A, 20.0, False))   # never anchored
        self.poll(TS, [ap_raw(last)], clock(BOOT_B, 15.0, True, wall=TS))                 # the next boot
        self.store.prune(TS - DAY)                    # boot A's poll comes before a poll still kept
        self.assertEqual(self.q("SELECT COUNT(*) FROM polls WHERE boot_id = ?", BOOT_A), [(1,)])
        self.store.prune(TS + DAY)                    # every later synced poll is old: it goes too
        self.assertEqual(self.q("SELECT COUNT(*) FROM polls"), [(0,)])

    def test_without_a_clock_the_v7_behaviour(self):
        # write_poll without clock data (tests, tools): synced by assumption
        self.store.write_poll(TS, HEALTH, [shape_device(AP_RAW, TS)], [], 42)
        self.assertEqual(self.q("SELECT clock_synced, boot_id FROM polls"), [(None, None)])
        self.assertEqual(self.q("SELECT COUNT(*) FROM ap_load_hourly"), [(1,)])


class Migration(unittest.TestCase):
    def test_v7_to_v8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "buffer.db")
            s = Store(path)
            s.write_poll(TS, HEALTH, [shape_device(AP_RAW, TS)], [shape_alert(ALERT_RAW)], 42)
            s.close()
            db = sqlite3.connect(path)
            # turn it into a v7 file: the v8 columns and tables gone
            for table, gone in (("polls", ("mono", "boot_id", "clock_synced", "chrony_offset")),
                                ("observations", ("poll_id",)), ("device_config_history", ("poll_id",)),
                                ("alerts", ("poll_id",)), ("devices", ("boot_id", "boot_last"))):
                for col in gone:
                    db.execute("ALTER TABLE %s DROP COLUMN %s" % (table, col))
            db.execute("DROP TABLE clock_anchors")
            db.execute("DROP TABLE ap_load_pending")
            db.execute("DELETE FROM meta WHERE key = 'buffer_id'")
            db.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
            db.commit()
            db.close()
            s = Store(path, boot_id=BOOT_A)
            self.assertEqual(s.get_meta("schema_version"), "8")
            self.assertIsNotNone(s.get_meta("buffer_id"))
            cols = {r[1] for r in s.db.execute("PRAGMA table_info(polls)")}
            self.assertTrue({"mono", "boot_id", "clock_synced", "chrony_offset"} <= cols)
            # the devices seen so far belong to the boot the migration runs in
            self.assertEqual(s.db.execute("SELECT boot_id, boot_last = last_seen FROM devices").fetchall(),
                             [(BOOT_A, 1)])
            self.assertEqual(s.db.execute("SELECT poll_id FROM observations").fetchall(), [(None,)])
            s.close()


if __name__ == "__main__":
    unittest.main()
