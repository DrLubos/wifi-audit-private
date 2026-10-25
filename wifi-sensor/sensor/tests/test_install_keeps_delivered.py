"""install_sensor.sh and a delivered configuration (server/docs/config-delivery.md
14, the developer's decision of 2026-10-25): while the applier's applied.json
names a running delivered configuration, the installer keeps the channel list
and hop rate of the current kismet_site.conf - one source of truth - instead
of CAPTURE_CHANNELS / the built-in list. Runs the installer's own function
through bash (WIFI_SENSOR_INSTALLER_SOURCE_ONLY=1). Standard library only."""

import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(HERE, "..", "..", "install_sensor.sh")

DELIVERED = ('# managed\nsource=wlan1:name=capture,type=linuxwifi,channels="1,6,11,36,40",'
             'channel_hoprate=2/sec\nalertbacklog=50\n')


def resolve(site_text=None, applied=True, capture_channels="", driver="rtw88_8821cu"):
    """KISMET_SOURCE_OPTS and the log after resolve_capture_channels()."""
    with tempfile.TemporaryDirectory() as d:
        site = os.path.join(d, "kismet_site.conf")
        if site_text is not None:
            with open(site, "w") as f:
                f.write(site_text)
        app = os.path.join(d, "applied.json")
        if applied:
            with open(app, "w") as f:
                f.write('{"version": 5, "sha256": "%s"}' % ("a" * 64))
        script = ('source "%s"; KISMET_SITE_CONF="%s"; APPLIED_CONFIG="%s"; CAPTURE_DRIVER=%s; '
                  'CAPTURE_CHANNELS="%s"; resolve_capture_channels; echo "OPTS=$KISMET_SOURCE_OPTS"'
                  % (INSTALLER, site, app, driver, capture_channels))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=dict(os.environ, WIFI_SENSOR_INSTALLER_SOURCE_ONLY="1"))
        return r.returncode, r.stdout + r.stderr


def bash4():
    r = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True)
    return r.stdout.strip().isdigit() and int(r.stdout) >= 4


@unittest.skipUnless(os.path.exists(INSTALLER), "install_sensor.sh not here")
@unittest.skipUnless(bash4(), "the installer needs bash >= 4 (the Pi's; macOS has 3.2)")
class KeepsTheDeliveredList(unittest.TestCase):
    def opts(self, out):
        return [line for line in out.splitlines() if line.startswith("OPTS=")][-1][5:]

    def test_kept_while_applied(self):
        rc, out = resolve(DELIVERED)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.opts(out), ',channels="1,6,11,36,40",channel_hoprate=2/sec')
        self.assertIn("delivered configuration v5", out)

    def test_capture_channels_is_ignored_then_with_a_warning(self):
        rc, out = resolve(DELIVERED, capture_channels="1,6,11")
        self.assertEqual(self.opts(out), ',channels="1,6,11,36,40",channel_hoprate=2/sec')
        self.assertIn("CAPTURE_CHANNELS", out)

    def test_without_applied_json_the_installers_list(self):
        rc, out = resolve(DELIVERED, applied=False)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.opts(out).startswith(',channels="1,1HT40+,2,'), out)
        self.assertEqual(self.opts(out).count(","), 89)              # the 89-entry list, no hoprate

    def test_applied_but_no_explicit_list_in_the_file_falls_back(self):
        rc, out = resolve("source=wlan1:name=capture,type=linuxwifi\n")
        self.assertTrue(self.opts(out).startswith(',channels="1,1HT40+,2,'), out)
        self.assertIn("falling back", out)

    def test_a_kept_list_must_still_pass_the_stride_rule(self):
        bad = 'source=wlan1:name=capture,type=linuxwifi,channels="%s"\n' % ",".join(str(c) for c in range(1, 39))
        rc, out = resolve(bad)                                       # 38 entries: stride 4, gcd 2
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
