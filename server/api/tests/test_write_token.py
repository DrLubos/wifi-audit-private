"""The write token check (app/v2/auth.py). Standard library only."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.v2 import auth  # noqa: E402

TOKEN = "0123456789abcdef0123456789abcdef"


class WriteToken(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(auth.check("Bearer " + TOKEN, TOKEN), auth.OK)
        self.assertEqual(auth.check("bearer  %s " % TOKEN, TOKEN + "\n"), auth.OK)   # .env newline

    def test_missing_wrong(self):
        self.assertEqual(auth.check(None, TOKEN), auth.MISSING)
        self.assertEqual(auth.check("", TOKEN), auth.MISSING)
        self.assertEqual(auth.check("Basic " + TOKEN, TOKEN), auth.MISSING)
        self.assertEqual(auth.check("Bearer ", TOKEN), auth.MISSING)
        self.assertEqual(auth.check("Bearer " + TOKEN[:-1] + "x", TOKEN), auth.WRONG)
        self.assertEqual(auth.check("Bearer " + TOKEN + "x", TOKEN), auth.WRONG)

    def test_disabled(self):
        self.assertEqual(auth.check("Bearer " + TOKEN, ""), auth.DISABLED)
        self.assertEqual(auth.check("Bearer " + TOKEN, None), auth.DISABLED)
        self.assertEqual(auth.check("Bearer short", "short"), auth.DISABLED)   # too short to be a secret

    def test_bearer(self):
        self.assertEqual(auth.bearer("Bearer abc"), "abc")
        self.assertIsNone(auth.bearer("Token abc"))


if __name__ == "__main__":
    unittest.main()
