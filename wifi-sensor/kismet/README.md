# Pinned Kismet packages

The sensor runs exactly one Kismet release, **2025-09-R1**: the installer and
the hop guard work around bugs of this release (the hop stride rule,
`block_channels`, `tracker_max_devices`, `set_channel.cmd` on a running
source - `../CLAUDE.md`), and a different release would change how the data
is captured. `install_sensor.sh` therefore installs `KISMET_VERSION` (default
`2025-09-R1`) and nothing else, and holds every Kismet package
(`apt-mark hold`), so neither `apt upgrade` nor a re-run of the installer
moves it. On pi-fri the hold is set since 2026-11-16.

The Kismet repository keeps only its newest release. While it still offers
2025-09-R1 the installer takes it from there; afterwards it installs the
saved `.deb` files:

```
sudo KISMET_DEB_DIR=/path/to/2025-09-R1 ./install_sensor.sh
```

- The files are **not in git** (21 packages, 26 MB). The copy taken from the
  Pi's apt cache on 2026-11-16 is on the developer's Mac in
  `~/Developer/wifi-audit-kismet/2025-09-R1/` (sha256 equal on both sides).
  Keep a second copy somewhere else.
- `<version>-<codename>-<arch>.sha256` here is their sha256 list; the
  installer refuses files that do not match it.
- They are built for **Debian 13 trixie, arm64**: a new Pi needs a
  trixie-based Raspberry Pi OS 64-bit Lite (older images stay downloadable
  from Raspberry Pi's archive). On a newer Debian the libraries they link
  against may be missing; then the fallback is building the `kismet-2025-09-R1`
  source tag (with swap, `-j1`/`-j2`; ../CLAUDE.md "Kismet install").
- Moving to another release on purpose: set `KISMET_VERSION`, add its sha256
  list here, run the installer, and record the switch-over in
  `../docs/findings.md` section 7 - the server records the version change it
  sees in the heartbeat as a dated `change` boundary (uploader 1.5).
