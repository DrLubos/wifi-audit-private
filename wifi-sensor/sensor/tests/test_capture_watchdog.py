"""Tests for sensor/capture-watchdog.sh: hub-port discovery, the power-cycle
safety checks and the escalation decision, against a fake sysfs tree that
mirrors the Pi 3B+ (Ethernet and the capture adapter on the same hub 1-1.1).

The script is sourced (only its functions are defined then); nothing touches
the real /sys. Run from wifi-sensor/:  python3 -m unittest discover -s sensor/tests
"""

import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "capture-watchdog.sh")


class FakeSysfs:
    """/sys/devices/... with hub 1-1.1: port1 = eth0 (lan78xx), port2 = wlan1."""

    def __init__(self, root):
        self.root = root
        self.hub = self.mkdir("devices/usb1/1-1/1-1.1")
        self.mkdir("devices/usb1/1-0:1.0/usb1-port1", disable="0", state="configured")
        for n in (1, 2, 3):
            self.mkdir("devices/usb1/1-1/1-1.1/1-1.1:1.0/1-1.1-port%d" % n,
                       disable="0", state="not attached")
        self.mkdir("class/net")
        self.add_device(1, "eth0")
        self.add_device(2, "wlan1")

    def mkdir(self, rel, **files):
        path = os.path.join(self.root, rel)
        os.makedirs(path, exist_ok=True)
        for name, value in files.items():
            with open(os.path.join(path, name), "w") as f:
                f.write(value + "\n")
        return path

    def port(self, n):
        return os.path.realpath(os.path.join(self.hub, "1-1.1:1.0", "1-1.1-port%d" % n))

    def device(self, n):
        return os.path.join(self.hub, "1-1.1.%d" % n)

    def add_device(self, n, iface=None, cls="00"):
        dev = self.mkdir(os.path.relpath(self.device(n), self.root), authorized="1", bDeviceClass=cls)
        os.symlink(os.path.relpath(self.port(n), dev), os.path.join(dev, "port"))
        with open(os.path.join(self.port(n), "state"), "w") as f:
            f.write("configured\n")
        if iface:
            intf = self.mkdir(os.path.relpath(os.path.join(dev, "1-1.1.%d:1.0" % n), self.root))
            netdir = self.mkdir("class/net/" + iface)
            os.symlink(os.path.relpath(intf, netdir), os.path.join(netdir, "device"))

    def remove_device(self, n, iface=None):
        subprocess.run(["rm", "-rf", self.device(n)], check=True)
        if iface:
            subprocess.run(["rm", "-rf", os.path.join(self.root, "class/net", iface)], check=True)
        with open(os.path.join(self.port(n), "state"), "w") as f:
            f.write("not attached\n")


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sys = FakeSysfs(os.path.realpath(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, code, env=None):
        """Run CODE in bash after sourcing the watchdog; (rc, stdout)."""
        e = dict(os.environ, WATCHDOG_SYSFS=self.sys.root, CAPTURE_IFACE="wlan1",
                 WATCHDOG_STATE_DIR=os.path.join(self.tmp.name, "run"),
                 WATCHDOG_PERSIST_DIR=os.path.join(self.tmp.name, "persist"))
        e.update(env or {})
        # Script as $1, not $0: sourced with $0 == the script, its main() would run.
        p = subprocess.run(["bash", "-c", 'source "$1"; ' + code, "bash", SCRIPT],
                           capture_output=True, text=True, env=e)
        return p.returncode, p.stdout.strip()

    # ---------------------------------------------------------- discovery --

    def test_port_of_iface(self):
        self.assertEqual(self.sh("usb_port_of_iface wlan1")[1], self.sys.port(2))
        self.assertEqual(self.sh("usb_port_of_iface eth0")[1], self.sys.port(1))

    def test_port_of_missing_iface_fails(self):
        self.sys.remove_device(2, "wlan1")
        rc, out = self.sh("usb_port_of_iface wlan1")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")

    def test_moved_adapter_is_followed(self):
        # Unplugged from port 2 and plugged into port 3 while running: same name.
        self.sys.remove_device(2, "wlan1")
        self.sys.add_device(3, "wlan1")
        self.assertEqual(self.sh("usb_port_of_iface wlan1")[1], self.sys.port(3))

    def test_port_child(self):
        self.assertEqual(self.sh('port_child "%s"' % self.sys.port(2))[1], self.sys.device(2))

    # -------------------------------------------------------------- safety --

    def safe(self, port, expected=""):
        return self.sh('port_safe "%s" wlan1 "%s"' % (port, expected))

    def test_adapter_port_is_safe(self):
        self.assertEqual(self.safe(self.sys.port(2), self.sys.device(2))[0], 0)

    def test_dead_adapter_port_is_safe(self):
        # The 2026-10-06 case: the device is gone, the port says "not attached".
        self.sys.remove_device(2, "wlan1")
        self.assertEqual(self.safe(self.sys.port(2), self.sys.device(2))[0], 0)

    def test_uplink_port_is_refused(self):
        rc, why = self.safe(self.sys.port(1))
        self.assertNotEqual(rc, 0)
        self.assertIn("eth0", why)

    def test_root_hub_port_is_refused(self):
        rc, why = self.safe(os.path.join(self.sys.root, "devices/usb1/1-0:1.0/usb1-port1"))
        self.assertNotEqual(rc, 0)
        self.assertIn("root-hub", why)

    def test_upstream_port_via_bus_link_is_refused(self):
        # /sys/bus/usb/devices/1-1:1.0/1-1-port1 style link to the port that feeds
        # the hub carrying eth0: must be resolved before the parent hub is derived.
        up = self.sys.mkdir("devices/usb1/1-1/1-1:1.0/1-1-port1", disable="0", state="configured")
        bus = self.sys.mkdir("bus/usb/devices")
        os.symlink(os.path.relpath(os.path.dirname(up), bus), os.path.join(bus, "1-1:1.0"))
        rc, why = self.safe(os.path.join(bus, "1-1:1.0", "1-1-port1"))
        self.assertNotEqual(rc, 0)
        self.assertIn("eth0", why)

    def test_port_with_a_hub_is_refused(self):
        self.sys.add_device(3, cls="09")
        rc, why = self.safe(self.sys.port(3))
        self.assertNotEqual(rc, 0)
        self.assertIn("hub", why)

    def test_port_with_another_device_is_refused(self):
        # Adapter moved away and something else plugged into its old port.
        self.sys.remove_device(2, "wlan1")
        self.sys.add_device(2)
        rc, why = self.safe(self.sys.port(2), "/some/other/1-1.1.9")
        self.assertNotEqual(rc, 0)
        self.assertIn("not the adapter", why)

    def test_port_without_disable_is_refused(self):
        os.remove(os.path.join(self.sys.port(2), "disable"))
        self.assertNotEqual(self.safe(self.sys.port(2))[0], 0)

    def test_uplink_behind_the_port_is_refused(self):
        # A NIC on a hub that hangs off port 3 is "behind" port 3.
        self.sys.add_device(3, cls="00")
        sub = self.sys.mkdir(os.path.relpath(os.path.join(self.sys.device(3), "1-1.1.3.1", "1-1.1.3.1:1.0"),
                                             self.sys.root))
        netdir = self.sys.mkdir("class/net/eth1")
        os.symlink(os.path.relpath(sub, netdir), os.path.join(netdir, "device"))
        rc, why = self.safe(self.sys.port(3))
        self.assertNotEqual(rc, 0)
        self.assertIn("eth1", why)

    # ------------------------------------------------------------ decision --

    def action(self, strikes, present, replugged=0, age=10 ** 9):
        return self.sh("next_action %d %d %d %d" % (strikes, present, replugged, age))[1]

    def test_escalation(self):
        cases = [
            # strikes, present, replugged, age -> action
            ((1, 1, 0, 10 ** 9), "none"),
            ((2, 1, 0, 10 ** 9), "restart"),
            ((2, 0, 0, 10 ** 9), "restart"),           # gone: Kismet restart first anyway
            ((3, 0, 0, 10 ** 9), "none"),
            ((4, 1, 0, 10 ** 9), "driver"),
            ((4, 0, 0, 10 ** 9), "port_cycle"),        # gone from the bus: no driver reload
            ((4, 0, 0, 120), "restart_only"),          # rate limit
            ((6, 1, 0, 10 ** 9), "replug"),
            ((6, 1, 1, 10 ** 9), "port_cycle"),        # re-plug already failed
            ((6, 0, 0, 10 ** 9), "port_cycle"),
            ((6, 0, 0, 240), "restart_only"),          # cycled at strike 4 of this round
            ((7, 1, 0, 10 ** 9), "replug"),            # strikes >= STRIKES_USB
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(self.action(*args), expected)

    def test_rate_limit_is_configurable(self):
        out = self.sh("next_action 4 0 0 120", {"PORT_CYCLE_MIN_INTERVAL": "60"})[1]
        self.assertEqual(out, "port_cycle")

    # -------------------------------------------------------------- action --

    def test_power_cycle_port(self):
        # systemctl/sleep stubbed as shell functions; the adapter "re-appears".
        self.sys.remove_device(2, "wlan1")
        state = os.path.join(self.tmp.name, "persist")
        os.makedirs(state, exist_ok=True)
        code = ('systemctl() { echo "systemctl $*"; }; '
                'sleep() { [[ -e %s/class/net/wlan1 ]] || mkdir -p %s/class/net/wlan1; }; '
                'IFACE=wlan1; PC_FILE=%s/cycled; power_cycle_port "%s"'
                % (self.sys.root, self.sys.root, state, self.sys.port(2)))
        rc, out = self.sh(code)
        self.assertEqual(rc, 0, out)
        self.assertIn("systemctl stop kismet", out)
        self.assertIn("systemctl start kismet", out)
        self.assertIn("wlan1 is back", out)
        with open(os.path.join(self.sys.port(2), "disable")) as f:
            self.assertEqual(f.read().strip(), "0")    # switched back on
        self.assertTrue(os.path.getsize(os.path.join(state, "cycled")) > 0)


if __name__ == "__main__":
    unittest.main()
