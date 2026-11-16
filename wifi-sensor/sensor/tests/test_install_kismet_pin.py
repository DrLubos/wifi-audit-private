"""install_sensor.sh pins Kismet (KISMET_VERSION, kismet/README.md): the
pinned release from the repository while it is there, else from the saved
.deb files (KISMET_DEB_DIR, sha256-checked against the manifest), never
another one; every Kismet package held. Runs the installer's own functions
through bash (WIFI_SENSOR_INSTALLER_SOURCE_ONLY=1) with apt / dpkg replaced
by shell functions that only record their calls - nothing is installed, and
the tests never run as root. Standard library only."""

import hashlib
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLER = os.path.join(HERE, "..", "..", "install_sensor.sh")
PKGS = ("kismet", "kismet-capture-linux-wifi", "kismet-core")

# apt / dpkg as shell functions (they shadow the real commands): state in $T
FAKES = r'''
dpkg() { if [[ ${1:-} == --print-architecture ]]; then echo arm64; fi; }
dpkg-query() {
  local pat=${@: -1} p                      # the installer runs with set -u: the last argument
  if [[ $pat == 'kismet*' ]]; then
    if [[ -s $T/installed ]]; then for p in $(cat $T/pkgs); do echo "ii  $p"; done; fi
  elif [[ -s $T/installed ]]; then
    echo "ii  $(cat $T/installed)"
  fi
  return 0
}
apt-cache() {
  if [[ $1 == madison && -s $T/repo ]]; then echo " $2 | $(cat $T/repo) | https://repo trixie/main arm64 Packages"; fi
}
apt-get() { echo "apt-get $*" >>$T/calls; if [[ -s $T/after ]]; then cp $T/after $T/installed; fi; }
apt-mark() {
  case $1 in
    showhold) cat $T/held 2>/dev/null; return 0 ;;
    hold) shift; echo "apt-mark hold $*" >>$T/calls; printf '%s\n' "$@" >>$T/held ;;
  esac
}
debconf-set-selections() { cat >/dev/null; }
helper_privileged() { return 0; }
getcap() { :; }
'''


def run(installed="", repo="", after="", held=(), deb_dir=None, manifest_lines=None, version="2025-09-R1"):
    """(returncode, output, calls) of install_kismet() in a sandbox."""
    with tempfile.TemporaryDirectory() as t:
        for name, text in (("installed", installed), ("repo", repo), ("after", after),
                           ("pkgs", "\n".join(PKGS)), ("held", "".join(h + "\n" for h in held))):
            with open(os.path.join(t, name), "w") as f:
                f.write(text)
        mdir = os.path.join(t, "manifests")
        os.mkdir(mdir)
        if manifest_lines is not None:
            with open(os.path.join(mdir, "%s-trixie-arm64.sha256" % version), "w") as f:
                f.write("".join(line + "\n" for line in manifest_lines))
        script = ('source "%s"; T="%s"; %s KISMET_MANIFEST_DIR="%s"; KISMET_VERSION="%s"; CODENAME=trixie; '
                  'KISMET_HELPER=/bin/sh; KISMET_DEB_DIR="%s"; install_kismet'
                  % (INSTALLER, t, FAKES, mdir, version, deb_dir or ""))
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env=dict(os.environ, WIFI_SENSOR_INSTALLER_SOURCE_ONLY="1"))
        calls = open(os.path.join(t, "calls")).read() if os.path.exists(os.path.join(t, "calls")) else ""
        return r.returncode, r.stdout + r.stderr, calls


def debs(d, version="2025-09-R1"):
    """Fake .deb files of PKGS in d and their manifest lines."""
    lines = []
    for p in PKGS:
        name = "%s_%s_arm64.deb" % (p, version)
        data = ("deb of %s" % p).encode()
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        lines.append("%s  %s" % (hashlib.sha256(data).hexdigest(), name))
    return lines


def bash4():
    r = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True)
    return r.stdout.strip().isdigit() and int(r.stdout) >= 4


@unittest.skipUnless(os.path.exists(INSTALLER), "install_sensor.sh not here")
@unittest.skipUnless(bash4(), "the installer needs bash >= 4 (the Pi's; macOS has 3.2)")
class PinsKismet(unittest.TestCase):
    def test_pinned_version_installed_nothing_installed_only_held(self):
        rc, out, calls = run(installed="2025-09-R1", repo="2026-03-R1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("apt-get", calls)                          # a newer repo release is ignored
        self.assertIn("apt-mark hold kismet kismet-capture-linux-wifi kismet-core", calls)

    def test_already_held_is_left_alone(self):
        rc, out, calls = run(installed="2025-09-R1", held=PKGS)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, "")
        self.assertIn("every Kismet package is held", out)

    def test_from_the_repository_with_the_exact_version(self):
        rc, out, calls = run(repo="2025-09-R1", after="2025-09-R1",
                             manifest_lines=["%s  %s_2025-09-R1_arm64.deb" % ("0" * 64, p) for p in PKGS])
        self.assertEqual(rc, 0, out)
        self.assertIn("kismet=2025-09-R1 kismet-capture-linux-wifi=2025-09-R1 kismet-core=2025-09-R1", calls)

    def test_gone_from_the_repository_without_saved_debs_refuses(self):
        rc, out, calls = run(repo="2026-03-R1")
        self.assertNotEqual(rc, 0)
        self.assertIn("KISMET_DEB_DIR", out)
        self.assertNotIn("apt-get", calls)

    def test_from_the_saved_debs(self):
        with tempfile.TemporaryDirectory() as d:
            lines = debs(d)
            rc, out, calls = run(repo="2026-03-R1", after="2025-09-R1", deb_dir=d, manifest_lines=lines)
            self.assertEqual(rc, 0, out)
            self.assertIn("%s/kismet-core_2025-09-R1_arm64.deb" % os.path.realpath(d), calls)
            self.assertIn("apt-mark hold", calls)

    def test_saved_debs_that_do_not_match_the_manifest_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            lines = debs(d)
            with open(os.path.join(d, "kismet-core_2025-09-R1_arm64.deb"), "ab") as f:
                f.write(b"tampered")
            rc, out, calls = run(repo="", deb_dir=d, manifest_lines=lines)
            self.assertNotEqual(rc, 0)
            self.assertIn("do not match", out)
            self.assertNotIn("apt-get", calls)

    def test_an_install_that_ends_on_another_version_fails(self):
        rc, out, calls = run(repo="2025-09-R1", after="2026-03-R1")
        self.assertNotEqual(rc, 0)
        self.assertIn("not at 2025-09-R1", out)


if __name__ == "__main__":
    unittest.main()
