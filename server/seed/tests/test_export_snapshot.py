"""export_snapshot.py: the compact copy and its meta stamp (standard library):

  cd server && python3 -m unittest discover -s seed/tests
"""

import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import export_snapshot as exp  # noqa: E402


class ExportSnapshot(unittest.TestCase):
    def test_copy_carries_buffer_size_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "buffer.db")
            db = sqlite3.connect(src)
            db.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                             "INSERT INTO meta VALUES ('schema_version', '7');"
                             "CREATE TABLE polls (ts INTEGER);")
            db.close()
            out = os.path.join(tmp, "snap.db")
            exp.snapshot(src, out)
            size = exp.mark(out, src, now=1790000000)
            self.assertEqual(size, os.path.getsize(src))
            meta = dict(sqlite3.connect(out).execute("SELECT key, value FROM meta"))
            self.assertEqual(meta["snapshot_source_bytes"], str(size))
            self.assertEqual(meta["snapshot_taken_at"], "1790000000")
            self.assertEqual(meta["schema_version"], "7")
            # the source is left alone
            self.assertNotIn("snapshot_source_bytes",
                             dict(sqlite3.connect(src).execute("SELECT key, value FROM meta")))


if __name__ == "__main__":
    unittest.main()
