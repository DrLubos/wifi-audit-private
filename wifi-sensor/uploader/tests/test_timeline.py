"""timeline.py: placing polls and wall-clock values on the true time line
(server/docs/live-upload.md 8.3, the field table). Standard library only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import timeline as tl  # noqa: E402

DAY = 86400
TRUE0 = 1760700000.0           # the true time at mono 0 of the boot
STALE0 = TRUE0 - 30 * DAY      # the stale clock at mono 0 (30 days behind)


def boot(source="ntp", anchor_mono=3600.0, unsynced=(20.0, 3570.0)):
    """A boot that ran unsynchronised from mono 20 to 3570 and got its anchor at
    mono 3600 (chrony stepped between)."""
    return tl.Boot("B", anchor=(TRUE0 + anchor_mono, anchor_mono), source=source,
                   stale_ts=int(STALE0 + 20), stale_mono=20.0, unsynced_mono=unsynced)


class Polls(unittest.TestCase):
    def test_fates(self):
        b = boot()
        self.assertEqual(tl.poll_fate({"clock_synced": 1}, None, "X"), tl.SEND)
        self.assertEqual(tl.poll_fate({"clock_synced": None}, None, "X"), tl.SEND)      # v7 row
        self.assertEqual(tl.poll_fate({"clock_synced": 0, "boot_id": "B"}, b, "B"), tl.SEND)
        unanchored = tl.Boot("B")
        self.assertEqual(tl.poll_fate({"clock_synced": 0, "boot_id": "B"}, unanchored, "B"), tl.HOLD)
        self.assertEqual(tl.poll_fate({"clock_synced": 0, "boot_id": "A"}, tl.Boot("A"), "B"), tl.UNVERIFIED)

    def test_place_poll(self):
        b = boot()
        self.assertEqual(tl.place_poll({"clock_synced": 1, "ts": 123}, b), (123, None))
        ts, off = tl.place_poll({"clock_synced": 0, "ts": int(STALE0 + 600), "mono": 600.0}, b)
        self.assertEqual(ts, int(TRUE0 + 600))
        self.assertAlmostEqual(off, 30 * DAY, delta=1)

    def test_delta_and_window(self):
        b = boot()
        self.assertAlmostEqual(b.delta, 30 * DAY)
        lo, hi = b.stale_window()
        self.assertLess(lo, STALE0 + 20)
        self.assertGreater(hi, STALE0 + 3570)


class Kismet(unittest.TestCase):
    def test_stale_values_shift_true_ones_stay(self):
        b = boot()
        stale = int(STALE0 + 1000)                   # a frame before the step
        self.assertEqual(tl.place_kismet(stale, b, 30 * DAY), stale + 30 * DAY)
        after_step = int(TRUE0 + 3700)               # a frame after the step, already true
        self.assertEqual(tl.place_kismet(after_step, b, None), after_step)
        # a synchronised poll can still carry a pre-step value (a frame minutes before)
        self.assertEqual(tl.place_kismet(stale, b, None), stale + 30 * DAY)
        self.assertIsNone(tl.place_kismet(None, b, None))
        self.assertIsInstance(tl.place_kismet(float(stale), b, None), float)

    def test_server_anchor_shifts_every_value(self):
        b = boot(source="server")
        self.assertIsNone(b.step_mono)
        v = int(STALE0 + 5000)
        self.assertEqual(tl.place_kismet(v, b, 30 * DAY), v + 30 * DAY)

    def test_small_or_negative_delta_moves_nothing(self):
        small = tl.Boot("S", anchor=(STALE0 + 5 + 100.0, 100.0), source="ntp", stale_ts=int(STALE0), stale_mono=0.0,
                        unsynced_mono=(0.0, 50.0))
        self.assertAlmostEqual(small.delta, 5.0)
        self.assertEqual(tl.place_kismet(int(STALE0 + 10), small, 5.0), int(STALE0 + 10))
        ahead = tl.Boot("A", anchor=(STALE0 - 3600 + 100.0, 100.0), source="ntp", stale_ts=int(STALE0),
                        stale_mono=0.0, unsynced_mono=(0.0, 50.0))
        self.assertLess(ahead.delta, 0)
        self.assertEqual(tl.place_kismet(int(STALE0 + 10), ahead, -3600.0), int(STALE0 + 10))

    def test_no_unsynced_polls_no_change(self):
        normal = tl.Boot("N", anchor=(TRUE0 + 30, 30.0), source="ntp")
        self.assertEqual(tl.place_kismet(int(TRUE0 + 100), normal, None), int(TRUE0 + 100))


class DeviceValues(unittest.TestCase):
    def test_placeable(self):
        b = boot()
        inside = int(STALE0 + 2000)
        self.assertEqual(tl.placeable(inside, [b]), inside + 30 * DAY)        # anchored: shifted
        outside = int(TRUE0 - DAY)
        self.assertEqual(tl.placeable(outside, [b]), outside)
        unanchored = tl.Boot("U", stale_ts=int(STALE0 + 20), stale_mono=20.0, unsynced_mono=(20.0, 600.0))
        self.assertIsNone(tl.placeable(int(STALE0 + 300), [unanchored]))      # cannot be placed yet
        self.assertIsNone(tl.placeable(None, [b]))
        self.assertEqual(tl.placeable(inside, []), inside)


if __name__ == "__main__":
    unittest.main()
