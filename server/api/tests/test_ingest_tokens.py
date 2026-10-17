"""Ingest tokens (app/ingest/tokens.py, docs/live-upload.md 6.1). Standard library only."""

import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.ingest import tokens  # noqa: E402

SECRET = "ab" * 32
TOKEN = "wsi1.7." + SECRET


class Tokens(unittest.TestCase):
    def test_format_and_id(self):
        self.assertEqual(tokens.make(7, SECRET), TOKEN)
        self.assertEqual(tokens.parse(TOKEN), 7)
        for bad in (None, "", "wsi1.7", "wsi1.x." + SECRET, "wsi1.0." + SECRET, "wsi2.7." + SECRET,
                    "wsi1.7." + SECRET[:-1], "wsi1.7." + SECRET.upper(), "wsi1.7." + SECRET + "\n",
                    " " + TOKEN, "wsi1.12345678901." + SECRET):
            self.assertIsNone(tokens.parse(bad), repr(bad))

    def test_digest_is_sha256_of_the_whole_token(self):
        # the same as ops/sensor_token.sh: printf %s "$token" | sha256sum
        self.assertEqual(tokens.digest(TOKEN), hashlib.sha256(TOKEN.encode("ascii")).digest())

    def test_matches(self):
        stored = hashlib.sha256(TOKEN.encode("ascii")).digest()
        self.assertTrue(tokens.matches(TOKEN, stored))
        self.assertTrue(tokens.matches(TOKEN, memoryview(stored)))     # psycopg returns bytea as memoryview/bytes
        self.assertFalse(tokens.matches("wsi1.7." + "cd" * 32, stored))
        self.assertFalse(tokens.matches(TOKEN, b""))

    def test_from_header(self):
        self.assertEqual(tokens.from_header("Bearer " + TOKEN), TOKEN)
        self.assertEqual(tokens.from_header("bearer   %s  " % TOKEN), TOKEN)
        self.assertIsNone(tokens.from_header(None))
        self.assertIsNone(tokens.from_header("Basic " + TOKEN))
        self.assertIsNone(tokens.from_header("Bearer"))

    def test_never_in_a_repr(self):
        # what the api logs about a token: its id, never the secret
        self.assertEqual(tokens.describe(TOKEN), "token 7")
        self.assertEqual(tokens.describe("garbage-" + SECRET), "a malformed token")
        self.assertNotIn(SECRET, tokens.describe(TOKEN))


if __name__ == "__main__":
    unittest.main()
