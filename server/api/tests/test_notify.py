"""E-mail notifications (app/notify.py): configuration, the conditions, one
mail per start and end, retries and the daily cap, the SMTP dialogue."""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import notify  # noqa: E402

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
GB = notify.GB
CFG = {"host": "smtp.example.org", "port": 587, "user": "u@example.org", "password": "pw",
       "from": "u@example.org", "to": ["me@example.org"], "offline_min": 15.0, "disk_min_gb": 2.0}


def sensor(**kw):
    s = {"name": "pi-fri", "last_contact_at": NOW - timedelta(seconds=20), "last_capture_at": NOW - timedelta(seconds=40),
         "disk_free_bytes": 8 * GB, "discarding": False}
    s.update(kw)
    return s


class Config(unittest.TestCase):
    def test_off_without_host_or_recipient(self):
        self.assertIsNone(notify.config({}))
        self.assertIsNone(notify.config({"NOTIFY_SMTP_HOST": "smtp.example.org"}))
        self.assertIsNone(notify.config({"NOTIFY_TO": "me@example.org"}))

    def test_defaults_and_lists(self):
        c = notify.config({"NOTIFY_SMTP_HOST": "smtp.example.org", "NOTIFY_TO": "a@x.org, b@x.org",
                           "NOTIFY_SMTP_USER": "u@x.org"})
        self.assertEqual((c["port"], c["from"], c["to"], c["offline_min"], c["disk_min_gb"]),
                         (587, "u@x.org", ["a@x.org", "b@x.org"], 15.0, 2.0))

    def test_bad_number(self):
        with self.assertRaises(ValueError):
            notify.config({"NOTIFY_SMTP_HOST": "h", "NOTIFY_TO": "t", "NOTIFY_SMTP_PORT": "x"})


class Conditions(unittest.TestCase):
    def keys(self, sensors, box=20 * GB):
        return sorted(notify.conditions(sensors, box, NOW, CFG, "Europe/Bratislava"))

    def test_all_well(self):
        self.assertEqual(self.keys([sensor()]), [])

    def test_no_contact(self):
        self.assertEqual(self.keys([sensor(last_contact_at=NOW - timedelta(minutes=16))]), ["contact:pi-fri"])
        self.assertEqual(self.keys([sensor(last_contact_at=None)]), ["contact:pi-fri"])

    def test_contact_without_capture(self):
        """The 2026-10-06 adapter loss: the uploader kept sending heartbeats."""
        self.assertEqual(self.keys([sensor(last_capture_at=NOW - timedelta(hours=1))]), ["capture:pi-fri"])

    def test_no_capture_alarm_in_discard_mode(self):
        self.assertEqual(self.keys([sensor(last_capture_at=NOW - timedelta(hours=1), discarding=True)]), [])

    def test_disks(self):
        self.assertEqual(self.keys([sensor(disk_free_bytes=GB)], box=GB), ["disk:box", "disk:pi-fri"])

    def test_no_ssid_or_mac_in_a_message(self):
        conds = notify.conditions([sensor(last_contact_at=NOW - timedelta(hours=3))], GB, NOW, CFG, "Europe/Bratislava")
        text = " ".join(s + " " + b for s, b in conds.values())
        self.assertNotRegex(text, r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
        self.assertIn("16 Nov 10:00 (09:00 UTC)", text)                  # CET in November


class TrackerTest(unittest.TestCase):
    def test_one_mail_at_start_one_at_end(self):
        t, out = notify.Tracker(), []
        cond = {"contact:pi-fri": ("sensor pi-fri: no contact", "body")}
        t.update(cond, NOW)
        t.flush(NOW, lambda s, b: out.append(s))
        t.update(cond, NOW + timedelta(minutes=1))                 # still on: nothing new
        t.flush(NOW + timedelta(minutes=1), lambda s, b: out.append(s))
        t.update({}, NOW + timedelta(hours=2))
        t.flush(NOW + timedelta(hours=2), lambda s, b: out.append(s))
        self.assertEqual(out, ["sensor pi-fri: no contact", "resolved - sensor pi-fri: no contact"])

    def test_failed_send_is_retried(self):
        t, out = notify.Tracker(), []

        def broken(s, b):
            raise OSError("connection refused")
        t.update({"disk:box": ("server disk almost full", "b")}, NOW)
        self.assertEqual(t.flush(NOW, broken), 0)
        self.assertEqual(t.flush(NOW, lambda s, b: out.append(s)), 1)
        self.assertEqual(out, ["server disk almost full"])

    def test_daily_cap(self):
        t, out, conds = notify.Tracker(), [], {}
        for rnd in range(3):                                         # 3 rounds of 15 new conditions
            conds.update({"k%d" % i: ("s%d" % i, "b") for i in range(rnd * 15, rnd * 15 + 15)})
            t.update(conds, NOW)
            t.flush(NOW, lambda s, b: out.append(s))
        self.assertEqual(len(out), notify.MAX_PER_DAY)                 # 45 started, 30 mailed
        t.flush(NOW + timedelta(days=1, seconds=1), lambda s, b: out.append(s))
        self.assertEqual(len(out), 45)

    def test_pending_is_bounded(self):
        t = notify.Tracker()
        t.update({"k%d" % i: ("s%d" % i, "b") for i in range(50)}, NOW)
        self.assertEqual(len(t.pending), notify.PENDING_MAX)


class FakeSMTP:
    log = []

    def __init__(self, host, port, timeout=None, **kw):
        FakeSMTP.log.append(("connect", host, port, "context" in kw))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.log.append(("login", user))

    def send_message(self, m):
        FakeSMTP.log.append(("send", m["Subject"], m["To"]))


class Send(unittest.TestCase):
    def setUp(self):
        FakeSMTP.log = []

    def test_starttls_on_587(self):
        notify.send(CFG, "test", "body", smtp=FakeSMTP)
        self.assertEqual(FakeSMTP.log, [("connect", "smtp.example.org", 587, False), ("starttls",),
                                        ("login", "u@example.org"), ("send", "[wifi-audit] test", "me@example.org")])

    def test_tls_from_the_start_on_465(self):
        notify.send(dict(CFG, port=465), "test", "body", smtp=FakeSMTP)
        self.assertEqual(FakeSMTP.log[0], ("connect", "smtp.example.org", 465, True))
        self.assertNotIn(("starttls",), FakeSMTP.log)


if __name__ == "__main__":
    unittest.main()
