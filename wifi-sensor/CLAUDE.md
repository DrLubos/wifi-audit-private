# CLAUDE.md — Wi-Fi Monitoring Sensor

Persistent context for the sensor half of the `wifi-audit` monorepo. Read at the
start of every session that touches `wifi-sensor/`.

This file lives in `wifi-sensor/`; every relative path and command below is
relative to that folder, not to the repository root. On the Pi the monorepo is
cloned as a whole and the installers are run from `wifi-sensor/`.

## What this is

Master's thesis project: a **fixed, passive Wi-Fi monitoring sensor** running on a
Raspberry Pi. It continuously observes the wireless environment and (later) forwards
data to a server for storage and analysis.

It continues a departmental research line (Dobis -> Stodola) **at the problem level**,
but the code here is written from scratch. Do NOT reuse or copy the earlier
`wifi-audit-tool` codebase.

The key difference from the prior work (Stodola's bachelor thesis): that was a
**mobile, operator-triggered, point-in-time audit** with active attacks (deauth +
dictionary cracking) and GPS tagging. This project is a **stationary, continuous,
purely passive monitor**. Its value comes from data that only continuous fixed
observation can produce: signal/RSSI stability over time, appearance/disappearance
timelines, activity patterns, long-run WIDS alerts, and changes over time (e.g. a
known AP changing encryption/channel/BSSID).

## Current status & scope (IMPORTANT - respect these boundaries)

Status: Milestone 1 and Milestone 2 (collector) are DONE and deployed on the Pi.

- **Milestone 1 - DONE:** Kismet runs on the Pi, capturing on the USB adapter
  (`wlan1`), logging a slim kismetdb (no frames, pruned by the log retention)
  and running its own WIDS alert engine.
- **Milestone 2 (collector) - DONE, deployed:** the collector polls Kismet's REST
  API every 30 s and writes compact time-series records to a local SQLite buffer.
  It is "dumb": read -> shape -> store. No detection logic.
- **Current phase: accumulating real data.** The headline detection / contribution
  has NOT been chosen yet - it will be selected from the accumulated data, not
  guessed in advance. Let the sensor run and collect before building anything more.
- **Out of scope until the contribution is chosen:** anomaly/threat detection,
  self-learning baselines, server upload, dashboard. Do not build these unless
  explicitly asked.
- **Never** in the deployed sensor: active attacks, deauth, handshake capture,
  password cracking, GPS, or a Flask web UI. This sensor is passive-only.

## Hardware

- Raspberry Pi 3B+ (arm64, **1 GB RAM** - memory is tight, avoid heavy builds).
- **Capture adapter (in use):** external USB adapter on `wlan1`, driver
  `rtw88_8821cu` (RTL8821CU), monitor mode confirmed. Selected via the installer's
  `CAPTURE_IFACE` config (never hardcoded); a replacement must also be USB,
  monitor-capable, and must not be the SSH uplink - the installer refuses otherwise.
  Avoid chipsets needing out-of-tree / DKMS drivers (e.g. AIC8800 - no usable
  mainline monitor driver).
- Built-in Wi-Fi (`wlan0`, brcmfmac) / Ethernet: management and uplink ONLY, never
  used for capture. `wlan0` cannot do monitor mode anyway.
- OS: **Raspberry Pi OS 64-bit Lite** (headless, no desktop; currently trixie-based -
  derive the apt codename dynamically, do not hardcode it).
- Connection: reachable as `ssh pi` (alias already configured on the Mac).

## Kismet install

- Install Kismet from its **official apt repo** (kismetwireless.net), NOT from source.
  Building modern C++ on 1 GB RAM is impractical. (Source only if a bleeding-edge
  feature is truly needed, and then add swap and use `-j1`/`-j2`.)
- Install so that Kismet runs **suid-root, not as full root**: only the small capture
  helper (e.g. `kismet_cap_linux_wifi`) is privileged; the main server runs as the
  normal user via the `kismet` group. The apt package sets this up; never `sudo kismet`.
- The packaged `kismet.service` ships with User=root - override it with a systemd
  drop-in so the server runs as the normal user (`pi`), group `kismet`.
- Add the user to the `kismet` group.
- Let Kismet manage the capture interface via its datasource (`source=wlanX`).
  Do not manually put the interface in monitor mode or fight Kismet over it.
- Status: `install_sensor.sh` was run with `CAPTURE_IFACE=wlan1`. Kismet's web/REST
  credentials live in `~/.kismet/kismet_httpd.conf` on the Pi (and, for local dev
  only, in a gitignored `.env` on the Mac) - never committed.
- **Cold-boot robustness** (added after a power-cut boot left Kismet "running" with
  0 packets for hours): `sensor/kismet-prestart.sh` runs as `ExecStartPre` of
  `kismet.service` (waits for the USB adapter and chrony, bounded; rfkill unblock;
  deletes a stale `${CAPTURE_IFACE}mon`; power save off) and
  `sensor/capture-watchdog.sh` runs from `wifi-sensor-capture-watchdog.timer`
  (templates in `systemd/`) every 2 min, checking Kismet's datasource
  `num_packets` via REST and escalating
  restart Kismet -> reload driver -> USB re-plug. Both touch only the capture
  adapter. Root cause of the original failure: the RTL8821CU enumerates as USB
  storage first and mode-switches ~15 s later, the clock steps by days at boot
  (no RTC), the first capture helper crashed and the re-open adopted a
  half-configured monitor VIF. Kismet reports such a source as running/no error.
- **Hung-server incident (2026-09-21 16:22 UTC):** the Kismet server aborted with
  glibc `corrupted size vs. prev_size` while the kernel logged 438 `rtw88_core`
  `WARNING`s (`phy.c:1876/2193`, `rtw_get_tx_power_params` via `rtw_set_channel`
  in `kismet_cap_linux_wifi`) within the same 41 s (the same WARN signature was
  back as a continuous storm on 2026-09-22/23, see below).
  Not an OOM-killer event (0 OOM-killer lines in 4 days of kernel journal, 23 MB of
  905 MB zram swap used). The "Kismet RSS 61 MB" noted at the time was measured
  right after the restart: Kismet's RSS grows ~21 KB per tracked device, and the
  run that ended at 16:17 that day stood at 392 MB with 17.5k devices after
  51.9 h (SYSTEM snapshots in its kismetdb) - memory was far from idle, even if
  a causal link to the heap abort is not shown. The process then hung in
  `deactivating` for 5 h because the packaged unit has `TimeoutStopSec=infinity`,
  so the watchdog's `systemctl restart kismet` blocked instead of escalating. The drop-in now sets
  `TimeoutStopSec=60`. A recurrence shows up as `polls.ok = 0` / `ds_running = 0`
  rows in the buffer - note the window if it happens during a staged experiment.
- **Disk-full incident (2026-09-22):** the 15 GB root filesystem filled up with
  11 GB of kismetdb logs (79 files, 69 of them empty from a crash loop; ~1.5 GB/day,
  ~93 % of each file the `packets` table, which nothing reads). Kismet's log
  stopped at 12:19 UTC, the collector's last good poll was 13:43:21, then the
  collector crash-looped on `disk I/O error` until the disk was freed; first good
  poll again 2026-09-23 09:26:58 (gap recorded in `docs/findings.md`). The buffer
  survived intact (`quick_check` ok after WAL replay - never delete `buffer.db-wal`).
  Fixes, all in `install_sensor.sh`:
  - `kismet_site.conf`: `kis_log_packets=false`, row timeouts for devices (1 d),
    snapshots and messages (7 d); `tracker_device_timeout=3600` bounds Kismet's
    RAM (at most ~2.1k devices are active per hour). **Do not set
    `tracker_max_devices`:** in Kismet 2025-09 it evicts the MOST recently seen
    devices (ascending last_time sort, removes `begin()+max..end`), leaves them in
    the views and uses a comparator that breaks on empty slots - a new rogue BSSID
    would vanish (`devicetracker.cc`, `timetracker_event()`).
  - `sensor/kismet-log-retention.sh`: hourly timer
    (`wifi-sensor-kismet-log-retention.timer`) + step 0 of the pre-start (so a crash
    loop cannot pile up empty logs). See the retention policy under Collector.
  - journald drop-in `/etc/systemd/journald.conf.d/60-wifi-sensor.conf`: persistent,
    `SystemMaxUse=200M`, `SystemKeepFree=1G`.
- **rtw88 WARN storm - NEXT TASK:** since at least 2026-09-22 21:03 UTC the kernel
  logs ~430 `rtw_get_tx_power_params` WARN traces per minute (`phy.c:1876/2193`, via
  `rtw_ops_config` -> `rtw_set_channel`, i.e. on channel changes - presumably
  Kismet's hopping; not yet confirmed) - continuously,
  not the 41 s burst of 2026-09-21. journald does not rate-limit kernel messages,
  so with the now persistent journal this means **continuous SD-card writes until
  the storm is fixed**: the 200 MB cap bounds the space, not the wear. Fixing (or
  suppressing at the source) the rtw88 storm is the next task.

## Data to read from Kismet (targets for the collector, Milestone 2)

Prefer the REST API (poll device snapshots) for the first version; consider the
Eventbus (push) for alerts later. Fields of interest:

- Identity: BSSID, SSID, manufacturer (OUI), channel/frequency.
- Encryption: `crypt_string` + `crypt_bitfield` (no separate RSN/AKM fields in this
  Kismet version), MFP capability, open/WPA2/WPA3.
- **Temporal (the point of this project):** RSSI current + history, first-seen /
  last-seen, packet counts over time.
- Clients and probe requests (what devices search for, and when).
- Kismet's native WIDS alerts (DEAUTHFLOOD, APSPOOF, CHANCHANGE, CRYPTODROP, ...).

## Collector (Milestone 2, part 1 - implemented)

- Repo layout: the deployed daemon is `collector/` (`collector.py`, `kismet_client.py`,
  `shape.py`, `store.py`, `tests/`); unit templates are in `systemd/`
  (`wifi-sensor-collector.service` + the capture-watchdog units); one-off read-only
  analysis scripts are in `analysis/` (not part of the service, see `analysis/README.md`);
  installers (`install_sensor.sh`, `install_collector.sh`) and `sensor.conf.example`
  stay at the repo root. `install_collector.sh` is run on the Pi with sudo after
  `install_sensor.sh`. Details and schema: `collector/README.md`.
- Installed to `/opt/wifi-sensor/collector` (analysis scripts to
  `/opt/wifi-sensor/analysis`), config `/etc/wifi-sensor/collector.conf`,
  buffer `/var/lib/wifi-sensor/buffer.db`. Credentials come from Kismet's own
  `~/.kismet/kismet_httpd.conf`.
- The installers never delete files on the Pi (the repo is git-cloned there; the
  installer only copies, configures and (re)starts). Leftovers are the user's call.
- **Runtime retention - the one approved exception (2026-09-23):**
  `sensor/kismet-log-retention.sh` is the only sensor component that deletes files.
  Its scope is strictly `${KISMET_LOG_TITLE}-*.kismet` and `-journal` files directly in
  `KISMET_LOG_DIR`: empty ones, ones older than `KISMET_LOG_KEEP_DAYS` (3), then the
  oldest while the logs exceed `KISMET_LOG_MAX_MB` (1024) or the filesystem has less
  than `KISMET_LOG_MIN_FREE_MB` (2048) free. It never deletes the log Kismet has open
  (read from `/proc/<pid>/fd` - Kismet is not dumpable, so the unit runs as root,
  sandboxed with `ReadWritePaths=` the log dir) nor the newest log, and never touches
  the buffer or anything else. Widening its scope, or adding any other deleting
  component, needs the developer's explicit approval.
- Tests: `cd wifi-sensor && python3 -m unittest discover -s collector/tests`
  (stdlib only, runs on the PC).
- Kismet quirks the code depends on: field-filtered responses return `0` (not `null`)
  for missing fields, so `0` is mapped to NULL for all non-counter fields, RSSI included;
  there are no `wpa_version`/RSN fields, only `crypt_string` + `crypt_bitfield`;
  `ietag_checksum`/`beacon_fingerprint` vary per beacon (stored per-observation, not
  treated as a stable identity); `/alerts/alerts.json` is 404.
- **Privacy rule:** never collect bystander PII - no `dot11.client.ipdata` (client IPs)
  and no WPS serial/model/manufacturer/device-name fields. Associations and probed
  SSIDs are fine.

## Operating rules for Claude Code

- Claude Code runs on the developer's **PC (Mac)**, not on the Pi.
- The Pi is reachable as `ssh pi` (configured in the PC's `~/.ssh/config`;
  IP, user, and key path live there - never in this repo).
- Claude Code may SSH to the Pi ONLY for **read-only exploration** to gather facts
  for writing scripts: e.g. `lsusb`, `iw list`, `iw dev`, `rfkill list`,
  `cat /etc/os-release`, `lspci`, `ip link`. Nothing that installs or changes state.
- Claude Code **writes** scripts (e.g. `install_sensor.sh`) locally into this repo.
  It must **NOT execute** the install script - the developer runs it on the Pi.
- The only local action on the PC is writing/editing files in this repo.
  No installs or other side effects on the PC.

## Repository philosophy

- This folder holds everything implemented on the Pi: the install scripts, the
  collector, configs, systemd units, docs. It is the developer's own work. The
  server half lives in `../server/` (placeholder until the contribution is chosen).
- Any prior/reference code (e.g. the old `wifi-audit-tool` fork) stays **outside**
  this repo and is used only as read-only reference when explicitly requested.
  Do not copy from it, and do not commit it here.

## Conventions

- Language: **English only** for all identifiers, comments, commit messages, filenames.
- Collector (Milestone 2 onward): **Python**. Standard `sqlite3` for the local
  buffer; `httpx` or `requests` for HTTP.
- **No hardcoded paths** - use config files / environment variables.
- Time: **chrony (NTP)** must be set up so timestamps are correct after outages.
- Run long-lived processes as **systemd services**, enabled on boot.
- Commit to git often; keep changes reviewable.

## Safety note for scripts

Any provisioning script must NOT disable or reconfigure the interface currently used
for SSH/uplink - that would cut the connection to the Pi. Touch only the dedicated
capture adapter, and confirm before changing network settings.
