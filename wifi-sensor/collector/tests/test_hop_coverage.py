"""Tests for hop_coverage.py (hop-list coverage recorded in polls.ds_hop_*)."""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hop_coverage import SiteConf, configured_channels, hop_health, same_hop_list  # noqa: E402

# The 89-entry rtw88_8821cu list (install_sensor.sh RTW88_8821CU_CHANNELS).
CONFIGURED = ("1,1HT40+,2,3,4,5,6,6HT40-,6HT40+,7,8,9,10,11,11HT40-,12,13,"
              "36,36HT40+,36VHT80,40,40HT40-,40VHT80,44,44HT40+,44VHT80,48,48HT40-,48VHT80,"
              "52,52HT40+,52VHT80,56,56HT40-,56VHT80,60,60HT40+,60VHT80,64,64HT40-,64VHT80,"
              "100,100HT40+,100VHT80,104,104HT40-,104VHT80,108,108HT40+,108VHT80,112,112HT40-,112VHT80,"
              "116,116HT40+,116VHT80,120,120HT40-,120VHT80,124,124HT40+,124VHT80,128,128HT40-,128VHT80,"
              "132,132HT40+,132VHT80,136,136HT40-,136VHT80,140,140VHT80,144,144HT40-,144VHT80,"
              "149,149HT40+,149VHT80,153,153HT40-,153VHT80,157,157HT40+,157VHT80,161,161HT40-,161VHT80,165"
              ).split(",")

SITE_CONF = """# kismet_site.conf - managed by install_sensor.sh
source=wlan1:name=capture,type=linuxwifi,channels="%s"
log_types=kismet
httpd_bind_address=127.0.0.1
""" % ",".join(CONFIGURED)


def source(hop, skip=4, shuffle=1, running=1):
    return {"kismet.datasource.hop_channels": hop, "kismet.datasource.hop_shuffle_skip": skip,
            "kismet.datasource.hop_shuffle": shuffle, "kismet.datasource.running": running}


class ParserTest(unittest.TestCase):
    def test_quoted_list(self):
        self.assertEqual(configured_channels(SITE_CONF), CONFIGURED)
        self.assertEqual(len(configured_channels(SITE_CONF)), 89)

    def test_no_channels_option_is_auto(self):
        self.assertIsNone(configured_channels("source=wlan1:name=capture,type=linuxwifi\n"))

    def test_no_source_line(self):
        self.assertIsNone(configured_channels("log_types=kismet\n"))

    def test_commented_source_is_ignored(self):
        text = '#source=wlan1:channels="1,6,11"\nsource=wlan1:name=capture\n'
        self.assertIsNone(configured_channels(text))

    def test_unquoted_single_channel(self):
        self.assertEqual(configured_channels("source=wlan1:channels=6,name=x\n"), ["6"])

    def test_several_sources_pick_by_name(self):
        text = ('source=wlan2:name=other,channels="1,6,11"\n'
                'source=wlan1:name=capture,channels="36,40"\n')
        self.assertEqual(configured_channels(text), ["1", "6", "11"])
        self.assertEqual(configured_channels(text, "capture"), ["36", "40"])


class MultisetTest(unittest.TestCase):
    def test_shuffled_list_is_the_same(self):
        live = CONFIGURED[:]
        random.Random(1).shuffle(live)
        self.assertNotEqual(live, CONFIGURED)
        self.assertTrue(same_hop_list(live, CONFIGURED))

    def test_case_is_ignored(self):
        self.assertTrue(same_hop_list([c.lower() for c in CONFIGURED], CONFIGURED))

    def test_missing_or_duplicated_entry_differs(self):
        self.assertFalse(same_hop_list(CONFIGURED[:-1], CONFIGURED))
        self.assertFalse(same_hop_list(CONFIGURED + ["1"], CONFIGURED))
        swapped = CONFIGURED[:-1] + ["1"]  # same length and set size, different counts
        self.assertFalse(same_hop_list(swapped, CONFIGURED))


class HopHealthTest(unittest.TestCase):
    def test_full_list_in_order(self):
        self.assertEqual(hop_health([source(CONFIGURED)], CONFIGURED), (89, 89, 1))

    def test_full_list_shuffled_is_ok(self):
        live = CONFIGURED[:]
        random.Random(7).shuffle(live)
        self.assertEqual(hop_health([source(live)], CONFIGURED), (89, 89, 1))

    def test_collapsed_to_channel_1(self):
        self.assertEqual(hop_health([source(["1"], skip=1)], CONFIGURED), (1, 1, 0))

    def test_stride_sharing_a_factor_is_degraded(self):
        live = CONFIGURED + ["165HT40-"]  # 90 entries, stride 4 -> 45 visited
        self.assertEqual(hop_health([source(live)], live), (90, 45, 0))

    def test_not_shuffling_visits_all(self):
        self.assertEqual(hop_health([source(CONFIGURED, skip=4, shuffle=0)], CONFIGURED), (89, 89, 1))

    def test_source_not_running_is_degraded(self):
        self.assertEqual(hop_health([source(CONFIGURED, running=0)], CONFIGURED), (89, 89, 0))

    def test_no_configured_list(self):
        self.assertEqual(hop_health([source(["1", "6"], skip=1)], None), (2, 2, None))

    def test_no_datasource(self):
        self.assertEqual(hop_health([], CONFIGURED), (None, None, None))
        self.assertEqual(hop_health(None, CONFIGURED), (None, None, None))


class SiteConfTest(unittest.TestCase):
    def test_rereads_on_change(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "kismet_site.conf")
            with open(path, "w") as f:
                f.write(SITE_CONF)
            site = SiteConf(path)
            self.assertEqual(len(site.channels()), 89)
            with open(path, "w") as f:
                f.write('source=wlan1:name=capture,channels="1,6,11"\n')
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            self.assertEqual(site.channels(), ["1", "6", "11"])

    def test_missing_file(self):
        self.assertIsNone(SiteConf("/nonexistent/kismet_site.conf").channels())


if __name__ == "__main__":
    unittest.main()
