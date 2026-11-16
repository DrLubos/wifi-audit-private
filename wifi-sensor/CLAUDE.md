# CLAUDE.md — Wi-Fi Monitoring Sensor

Persistent context for the sensor half of the `wifi-audit` monorepo. Read at the
start of every session that touches `wifi-sensor/`.

This file lives in `wifi-sensor/`; every relative path and command below is
relative to that folder, not to the repository root. On the Pi the monorepo is
cloned as a whole and the installers are run from `wifi-sensor/`.

## What this is

Master's thesis project: a **fixed, passive Wi-Fi monitoring sensor** running on a
Raspberry Pi. It continuously observes the wireless environment and uploads every
poll to the server (`../server/`, live since 2026-10-17) for storage and analysis.

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
- **The sensor stays "dumb": read -> shape -> store.** Detection, baselines and the
  evaluation live on the server (`../server/detection/`, `ap_baselines`,
  `../server/evaluation/`); never add detection logic to the sensor.
- **Live upload (approved 2026-10-17, live since 2026-10-17 20:46:38 UTC;
  rollout record in its section 17): `../server/docs/live-upload.md`** -
  collector buffer v8 (clock correction
  data: monotonic time, boot id, clock-synced flag, one anchor per boot; N11
  saves the boot clock) and a separate uploader daemon that reads the buffer
  read-only and sends each poll to the server (`uploader/`, installed by
  `install_uploader.sh` to `/opt/wifi-sensor/uploader`, config
  `/etc/wifi-sensor/uploader.conf`, token `/etc/wifi-sensor/ingest.token`,
  unit `wifi-sensor-uploader.service`; tests `python3 -m unittest discover -s
  uploader/tests`; `INGEST_URL` is `https://34-42-184-97.sslip.io/api/ingest/v1`, TLS
  always verified). It never writes the buffer; its own state (cursor,
  counters, server anchors) is a JSON file, and a lost one restarts from the
  server's last poll.
- **Configuration delivery (built 2026-10-25, ON since 14:44:50; the applier
  restarts Kismet to apply, ~3-5 s without capture per change;
  `../server/docs/config-delivery.md`):** collector buffer v9
  (`polls.config_version` from `/var/lib/wifi-sensor-config/applied.json`),
  uploader 1.2 (`uploader/configsync.py`: a delivered configuration goes to
  `/var/lib/wifi-sensor-uploader/config/desired.json` only with
  `CONFIG_APPLY=1` in `uploader.conf`), and the root applier
  `sensor/config_apply.py` (`install_config_apply.sh`: `.path`-triggered apply
  with verify and automatic rollback, a recover unit before Kismet at boot,
  hourly capabilities). **`CONFIG_APPLY=1` since 2026-10-25 (the developer's
  decision): every configuration saved in Settings -> Sensors changes
  capture and is a dated boundary - a findings section 7 row for each.**
  The first staged test failed (Kismet refused the live `set_channel.cmd`,
  design section 15); the second, by Kismet restart, passed (section 16).
  Never change `CONFIG_APPLY` or save a test configuration without the
  developer's OK. While a delivered configuration runs, `install_sensor.sh`
  keeps its hop list (`applied.json`; design section 14, built 2026-10-25).
- **Client retention (2026-11-16):** the collector deletes the client data not
  seen for `RETENTION_DAYS` (14): client devices (never an AP) with their
  frequency histograms, `associations`, `client_bssids`, `probes`
  (`store.prune_clients()`, at most 5,000 rows a table per pass). Until then
  they were kept for the life of the sensor; everything up to the change was
  copied to the server first (a snapshot import with `--with-client-tables`).
  None of it is uploaded. A dated dataset change (findings section 7).
- **Uploader 1.5 (2026-11-16)** reports the installed Kismet version in the
  heartbeat; the server records a change of it as a dated boundary.
- **Never** in the deployed sensor: active attacks, deauth, handshake capture,
  password cracking, GPS, or a Flask web UI. This sensor is passive-only.

## Hardware

- Raspberry Pi 3B+ (arm64, **1 GB RAM** - memory is tight, avoid heavy builds).
- **Capture adapter (in use):** external USB adapter on `wlan1`, driver
  `rtw88_8821cu` (RTL8821CU), monitor mode confirmed. Selected via the installer's
  `CAPTURE_IFACE` config (never hardcoded); a replacement must also be USB,
  monitor-capable, and must not be the SSH uplink - the installer refuses otherwise.
  The hop list comes from the installer's `CAPTURE_CHANNELS` (built-in list only for
  `rtw88_8821cu`; a replacement adapter falls back to Kismet's autodetected list -
  check it for invalid entries like `165HT40-`, see the WARN storm below).
  Avoid chipsets needing out-of-tree / DKMS drivers (e.g. AIC8800 - no usable
  mainline monitor driver).
- Built-in Wi-Fi (`wlan0`, brcmfmac) / Ethernet: management and uplink ONLY, never
  used for capture. `wlan0` cannot do monitor mode anyway.
- OS: **Raspberry Pi OS 64-bit Lite** (headless, no desktop; currently trixie-based -
  derive the apt codename dynamically, do not hardcode it).
- Connection: reachable as `ssh pi` (alias already configured on the Mac).

## Kismet install

The rules; the incidents behind them (dates, measurements, root causes) are in
`docs/incidents.md`, the dated data effects in `docs/findings.md` (6, 7).

- Install Kismet from its **official apt repo** (kismetwireless.net), NOT from source
  (modern C++ on 1 GB RAM is impractical; source only if truly needed, then with swap
  and `-j1`/`-j2`).
- **Pinned to 2025-09-R1** (`KISMET_VERSION` in `install_sensor.sh`; every Kismet
  package `apt-mark hold` since 2026-11-16; the saved `.deb` files and their sha256
  list: `kismet/README.md`). The workarounds below depend on this release's bugs;
  another release is a dataset boundary (findings section 7) and needs the
  developer's OK.
- **suid-root, not full root**: only the capture helper (`kismet_cap_linux_wifi`) is
  privileged; the server runs as `pi` (a member of group `kismet`) via a systemd drop-in over
  the packaged `User=root` unit. Never `sudo kismet`. Kismet manages the capture
  interface through its datasource (`source=wlanX`) - never put the interface into
  monitor mode by hand.
- Status: `install_sensor.sh` was run with `CAPTURE_IFACE=wlan1` (a gitignored
  `sensor.conf` on the Pi; the prompt offers USB adapters only, monitor-capable
  first). Kismet's web/REST credentials live in `~/.kismet/kismet_httpd.conf` on the
  Pi (and, for local dev only, in a gitignored `.env` on the Mac) - never committed.
- **Web UI / REST API on loopback only** (`KISMET_HTTPD_BIND`, default `127.0.0.1`).
  Everything on the Pi uses 127.0.0.1; from the Mac `ssh -L 2501:127.0.0.1:2501 pi`
  and `http://localhost:2501/` (remote side 127.0.0.1: on the Pi localhost resolves
  to ::1 first). It listened on `0.0.0.0:2501` until 2026-09-27; the login was kept
  (developer's decision). The installer warns before "Keep it?" when the previous
  config was exposed and restarts the collector whenever the login changes. Do not
  put the Pi's addresses into the repo.
- Listening sockets (audit 2026-09-27, `docs/incidents.md`): disabling avahi-daemon
  (mDNS on the campus LAN, not needed) is the developer's call.
- **Cold boot**: `sensor/kismet-prestart.sh` (ExecStartPre of `kismet.service`:
  waits for the USB adapter and chrony, bounded; rfkill unblock; deletes a stale
  `${CAPTURE_IFACE}mon`; power save off) and `sensor/capture-watchdog.sh` (timer every
  2 min: Kismet's datasource `num_packets`, escalating restart Kismet -> reload driver
  -> USB re-plug -> hub-port power-cycle, below). Both touch only the capture adapter.
  The RTL8821CU enumerates as USB storage first and mode-switches ~15 s later; the
  clock steps by days at boot (no RTC).
- The `kismet.service` drop-in sets **`TimeoutStopSec=60`** (the packaged
  `infinity` hung a restart for 5 h, 2026-09-21). A recurrence of a hung or crashed
  server shows as `polls.ok = 0` / `ds_running = 0` rows - note the window if it
  happens during a staged experiment.
- **Disk and memory bounds** (disk-full incident 2026-09-22): `kismet_site.conf`
  `kis_log_packets=false`, row timeouts for devices (1 d), snapshots and messages
  (7 d), `tracker_device_timeout=3600`. **Never set `tracker_max_devices`** (2025-09
  evicts the most recently seen devices - a new rogue BSSID would vanish).
  `sensor/kismet-log-retention.sh` (hourly timer + step 0 of the pre-start; scope:
  Collector below). The journald drop-in `60-wifi-sensor.conf` (persistent,
  `SystemMaxUse=200M`, `SystemKeepFree=1G`) overrides Raspberry Pi OS's volatile
  journal; `configure_journald()` ensures `/var/log/journal` and flushes. Journal
  lines before each boot's "clock synchronised" pre-start line carry the stale
  clock. Never delete `buffer.db-wal`.
- **Hop list**: explicit, from the installer's `CAPTURE_CHANNELS`; the default for
  `rtw88_8821cu` is Kismet's 91-entry autodetected list minus `165HT40-` (the
  2026-09-22..26 rtw88 WARN storm) and `140HT40-`, same order, **89 entries**;
  `auto` for other drivers - check such a list for invalid entries. **Never use
  `block_channels`** in 2025-09-R1 (it erases the first entry that does NOT match).
  While a delivered configuration runs (`/var/lib/wifi-sensor-config/applied.json`)
  the installer keeps the current `channels=` / `channel_hoprate=`. Any hop-list
  change alters per-channel dwell: a findings section 7 row with its effective time.
- **Hop stride rule**: the capture helper hops with a stride derived from the list
  length only; a list must satisfy gcd(N, stride) = 1 - **keep lists prime** (a
  re-apply continues the stride search from the carried-over value). The installer
  replays the search (`hop_stride_ok`) and refuses a bad explicit list;
  `verify_kismet()` checks the live stride. (90 entries halved coverage 2026-09-26/27.)
- **A capture-helper crash pins the sensor to channel 1** (Kismet re-opens the
  source from an unquoted definition; the watchdog does not see it - a channel-1
  capture keeps the counter moving). **The hop guard** (`sensor/hop_guard.py`,
  `wifi-sensor-hop-guard.service`, installed by `install_sensor.sh`, the sensor user)
  compares the live hop list every 10 s with the `channels=` list of the `source=`
  line in `kismet_site.conf` **as an unordered multiset** (same entries and count,
  case-insensitive - Kismet shuffles) and on a mismatch re-applies it (`POST
  /datasource/by-uuid/<uuid>/set_channel.cmd`: `channels`, `rate`, `shuffle`; admin
  login). It never restarts Kismet, logs only its own actions, backs off to 60 s
  after 3 failed or non-sticking re-applies. Degraded polls: `polls.ds_hop_ok = 0`
  (buffer v4). If the guard is stopped, check `kismet.datasource.hop_channels` (or
  `dwell_poll.py`) after any "IPC connection closed". Polling rather than the
  eventbus: no stdlib websocket client, and a listener needs the periodic check
  anyway. Tests: `python3 -m unittest discover -s sensor/tests`.
- **Open item - capture helper restarts** (~13 a day; "did not get PING from Kismet
  for over 15 seconds" while hopping, or the adapter dropping off the bus): keep
  counting (`journalctl -u kismet | grep "IPC connection closed"`, the delta of
  `retry_attempts`) and look at the kernel log around each crash (findings section 9).
- **Adapter loss** (2026-10-06..10, 4 days without capture; the adapter stopped
  enumerating): power-cycling its hub port brought it back. Never touch port 1 (the
  `lan78xx` Ethernet uplink) or the whole hub `1-1.1`. Suspect the hardware (cable,
  connector, adapter); a dead adapter stays dead - replace it. **The watchdog does
  this itself** (`sensor/capture-watchdog.sh`):
  - The port is **learned, not configured**: on every run while `wlan1` exists,
    `wlan1 -> USB device -> its 'port' link` (resolved sysfs path), saved in
    `/var/lib/wifi-sensor-watchdog/` (systemd `StateDirectory`, survives reboots). A
    replugged adapter gets the lowest free name (`wlan1` again unless another Wi-Fi
    USB device took it); Kismet re-opens its `wlan1` source by itself.
  - **When:** never on the first failure. Strike 2 (~4 min) restarts Kismet; strike
    4 (~8 min) reloads the driver if the adapter is on the bus, else power-cycles the
    port; strike 6 (~12 min) re-plugs (`authorized` 0/1) the first time,
    power-cycles if the adapter is gone or a re-plug already failed in this outage;
    then the round starts over. At most one power-cycle per
    `PORT_CYCLE_MIN_INTERVAL` (600 s), each logged.
  - **Safety (`port_safe`, tests in `sensor/tests/test_capture_watchdog.py`):**
    refuses a root-hub port, any port behind which another network interface lives
    (compared on resolved device paths), a port holding a USB hub, and a port now
    holding a device other than the remembered adapter. Kismet is stopped around the
    power-cycle; the script waits up to 45 s for `wlan1`.
  - Whether `disable` really cuts VBUS depends on the hub; on this Pi 3B+ it brought
    the adapter back once (2026-10-10).

## Data read from Kismet

The collector polls the REST API (device snapshots, alerts); the eventbus is not
used. Fields of interest:

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
- Inside the buffer the collector's retention deletes rows (not files): the time
  series older than `RETENTION_DAYS`, and since 2026-11-16 (approved by the
  developer) the client rows not seen for that long (`store.prune_clients()`).
- Tests: `cd wifi-sensor && python3 -m unittest discover -s collector/tests`
  (stdlib only, runs on the PC).
- Kismet quirks the code depends on: field-filtered responses return `0` (not `null`)
  for missing fields, so `0` is mapped to NULL for all non-counter fields, RSSI included;
  there are no `wpa_version`/RSN fields, only `crypt_string` + `crypt_bitfield`;
  `ietag_checksum`/`beacon_fingerprint` vary per beacon (never a stable identity; not
  requested from buffer v6); `/alerts/alerts.json` is 404.
- **Slim observations (buffer schema v6; effective time in `docs/findings.md`
  section 7):** an observation row is written with `ts`, `key`, `last_time`,
  `rssi`/`rssi_floor`, `pk_total`, `pk_data`, `disconnects`,
  `disconnects_last`, `bss_timestamp` only; the other columns stay in the table
  as NULL (no rewrite on the Pi; retention ages the old values out; roll back by
  setting `meta.schema_version` to 5). AP load is the latest value on `devices`
  (`cur_n_clients`, `cur_qbss_stations`, `cur_util_pct`, `cur_at`), written by
  the devices upsert every poll and never through `merge_config` or
  `device_config_history`. A client's BSSID is in `client_bssids` (deduplicated
  like `associations`). The dropped fields are not requested from Kismet. An
  AP's band comes from `devices.adv_channel` (analysis scripts, server).
- **AP load history (buffer schema v7; effective time in `docs/findings.md`
  section 7):** `ap_load_hourly` = one row per AP, UTC hour and advertised
  channel with poll count, max/sum of the client count and - only for polls
  whose beacon carried a BSS Load element (`dot11e_qbss`) - max/sum of the
  AP-reported station count and utilisation. Written with the observation row
  (new packets only), pruned with it. Without the element the station count and
  utilisation are NULL (Kismet reports 0), also in `devices.cur_*`. The server
  loads it into its own `ap_load_hourly` (dashboard v2 clients/utilization
  charts). Roll back by setting `meta.schema_version` to 6.
- **Dataset rules (buffer schema v5, deployed 2026-09-27 19:57:29 UTC;
  `docs/findings.md` sections 7 and 11):**
  - **RSSI floors are censored values, not levels.** The RTL8821CU reports
    -106 dBm as its CCK floor (every 2.4 GHz beacon; `rtw8821c.c`:
    `lna_gain_table[lna] - 2 * vga`, minimum -106) and -120 dBm as its OFDM
    clamp. One definition: `collector/dataset_rules.py` (`RSSI_FLOOR_DBM`,
    `rssi_value()`, `sql_rssi()`), used by `shape.py` and every analysis script.
    From v5 the collector stores a floor as NULL in `rssi`/`rssi_min`/`rssi_max`
    and sets `observations.rssi_floor = 1`; older rows keep the raw value, so
    readers always filter. Never drop floors silently from a per-AP statistic:
    report the floor share (a weak AP's median is biased upward without them).
    A different capture adapter needs its floors re-derived (and a section-7
    row). The server has the same list in `server/schema.sql` (`rssi_valid()`),
    kept in sync by `collector/tests/test_dataset_rules.py`.
  - **AP configuration is merged field by field** (`store.merge_config`): NULL
    from Kismet = unknown, keep the stored value; only known -> different known
    is a change. Before v5 the alternating empty/real beacon record wrote a
    history row on almost every poll for some APs (42 % of all history rows);
    after v5 history fell from ~540 to ~48 rows/h, almost all real channel
    changes. Known residual: an AP first seen without a beacon record gets
    `cloaked`/`crypt_bits`/`mfp` = 0 (`_flag`/`_counter`), so its first record
    writes one history row.
  - **`observations.freq_khz` is not the reception channel of `rssi`** (Kismet's
    device frequency, set from other frames); the AP's channel is
    `devices.adv_channel`. Not written from buffer v6 on.
- **Privacy rule:** never collect bystander PII - no `dot11.client.ipdata` (client IPs)
  and no WPS serial/model/manufacturer/device-name fields. Associations and probed
  SSIDs are fine.

## Operating rules for Claude Code

- Claude Code runs on the developer's **PC (Mac)**, not on the Pi.
- The Pi is reachable as `ssh pi` (configured in the PC's `~/.ssh/config`;
  IP, user, and key path live there - never in this repo).
- Rules set 2026-10-17 (live-upload rollout mode; they replace the earlier
  read-only rule). Claude Code writes scripts locally into this repo and may
  run these on the Pi itself:
  - read-only exploration (e.g. `lsusb`, `iw dev`, `ip link`, `journalctl`,
    `systemctl status`, reading the buffer with `mode=ro`);
  - `git pull` in the clone (`~/wifi-audit-private`), the unit tests;
  - the live-upload installers `install_collector.sh` (collector v8) and
    `install_uploader.sh`, the N11 clock-save timer of step 3,
    `uploader.py --check`, enabling the uploader, writing the ingest token into
    `/etc/wifi-sensor/uploader.conf` (from `ops/sensor_token.sh` on the server
    in one pipeline - never printed).
  - sudo: the sensor user has passwordless sudo since 2026-10-18
    (`/etc/sudoers.d/020-pi-nopasswd`, `sudo -n -l`: `(ALL) NOPASSWD: ALL`;
    until 2026-11-15 this file still said it needs the developer's password).
    That widens nothing: it is used only for the steps listed above; for any
    other step that needs root, stop and hand the developer the exact
    commands.
- **Never on the Pi:** change network, Tailscale, SSH, firewall or chrony
  configuration (beyond the N11 timer), reboot it, or change capture settings
  beyond the planned collector v8. The sensor stays strictly passive.
- **Stays the developer's:** the clock-validation runs
  (`docs/clock-validation.md` - they reboot the Pi: ask first, for each run)
  and `install_sensor.sh` changes outside the live-upload plan.
- On the Mac: writing/editing files in this repo, running the tests. No
  installs or other side effects on the PC.

## Repository philosophy

- This folder holds everything implemented on the Pi: the install scripts, the
  collector, configs, systemd units, docs. It is the developer's own work. The
  server half lives in `../server/`.
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
