"""sensorinfo.kismet_version(): the installed Kismet version from dpkg's status file."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import sensorinfo  # noqa: E402

STATUS = """Package: kismet
Status: install ok installed
Architecture: arm64
Version: 2025-09-R1
Depends: kismet-core (= 2025-09-R1)

Package: kismet-core
Status: install ok installed
Priority: optional
Version: 2025-09-R1

Package: libc6
Status: install ok installed
Version: 2.41-12
"""


class KismetVersion(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "status")
        sensorinfo._kismet_cache.clear()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mtime=None):
        with open(self.path, "w") as f:
            f.write(text)
        if mtime is not None:
            os.utime(self.path, ns=(mtime, mtime))

    def test_reads_kismet_core(self):
        self.write(STATUS)
        self.assertEqual(sensorinfo.kismet_version(self.path), "2025-09-R1")

    def test_meta_package_alone(self):
        self.write("Package: kismet\nStatus: install ok installed\nVersion: 2023-07-R1\n")   # no trailing blank line
        self.assertEqual(sensorinfo.kismet_version(self.path), "2023-07-R1")

    def test_core_wins_over_the_meta_package(self):
        self.write("Package: kismet\nStatus: install ok installed\nVersion: 1\n\n"
                   "Package: kismet-core\nStatus: install ok installed\nVersion: 2\n\n")
        self.assertEqual(sensorinfo.kismet_version(self.path), "2")

    def test_removed_package_is_not_installed(self):
        self.write("Package: kismet-core\nStatus: deinstall ok config-files\nVersion: 2025-09-R1\n\n")
        self.assertIsNone(sensorinfo.kismet_version(self.path))

    def test_missing_file(self):
        self.assertIsNone(sensorinfo.kismet_version(os.path.join(self.dir.name, "nope")))

    def test_reread_only_when_the_file_changes(self):
        self.write(STATUS, mtime=1_000_000_000)
        self.assertEqual(sensorinfo.kismet_version(self.path), "2025-09-R1")
        self.write(STATUS.replace("2025-09-R1", "2026-03-R1"), mtime=1_000_000_000)    # same mtime: cached
        self.assertEqual(sensorinfo.kismet_version(self.path), "2025-09-R1")
        self.write(STATUS.replace("2025-09-R1", "2026-03-R1"), mtime=2_000_000_000)
        self.assertEqual(sensorinfo.kismet_version(self.path), "2026-03-R1")


if __name__ == "__main__":
    unittest.main()
