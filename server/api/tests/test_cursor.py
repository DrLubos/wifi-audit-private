"""Keyset cursors of the event feed (app/v2/cursor.py). Standard library only."""

import base64
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import cursor  # noqa: E402

TS = datetime(2026, 10, 4, 15, 39, 52, 526998, tzinfo=timezone.utc)


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class Cursor(unittest.TestCase):
    def test_round_trip(self):
        tok = cursor.encode(TS, 271)
        self.assertEqual(cursor.decode(tok), (TS, 271))
        self.assertRegex(tok, r"^[A-Za-z0-9_-]+$")             # URL-safe, no padding

    def test_microseconds_and_old_dates_survive(self):
        for ts in (datetime(1970, 1, 1, tzinfo=timezone.utc),
                   datetime(1969, 12, 31, 23, 59, 59, 1, tzinfo=timezone.utc),
                   datetime(2026, 9, 15, 13, 53, 7, 1, tzinfo=timezone.utc)):
            self.assertEqual(cursor.decode(cursor.encode(ts, 1)), (ts, 1))

    def test_tampering_is_rejected(self):
        tok = cursor.encode(TS, 271)
        bad = [
            "", "x", tok + "A", tok + "=", "!!!!", "a" * 65,
            b64("1759592392526998"),            # no id
            b64("1759592392526998.0"),          # id 0
            b64("1759592392526998.-1"),
            b64("01759592392526998.271"),       # not canonical
            b64("1759592392526998.0271"),
            b64("1759592392526998.271.1"),
            b64("abc.271"),
            b64("99999999999999999.271"),       # year > 9999
            b64("1.%d" % 2 ** 63),               # id beyond bigint
        ]
        for t in bad:
            with self.subTest(t=t), self.assertRaises(ValueError):
                cursor.decode(t)

    def test_naive_ts_refused(self):
        with self.assertRaises(ValueError):
            cursor.encode(datetime(2026, 10, 4), 1)


if __name__ == "__main__":
    unittest.main()
