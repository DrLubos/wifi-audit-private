# Dashboard v2 – implementation plan

Status: Phase 0 output, 2026-10-11. Nothing in this file is built. Each phase
starts only after the developer's go-ahead; the proposed design changes
(section 2) go into `SPEC.md` once they are approved (README rule).

Inputs: `SPEC.md` (approved 2026-10-10), `boards/*.dc.html`, the three
`CLAUDE.md` files, the code at `8478e2e`, and the live state below (section 1).

Contents: 1 Live state · 2 Proposed SPEC changes · 3 Gap analysis · 4 Schema 8
and 9 · 5 security_level() · 6 Events model · 7 API · 8 Frontend · 9 Collector
v7, importer, backfill · 10 Phases · 11 Later phases (designed, not built) ·
12 Risks · 13 Open questions

---

## 1. Live state (verified read-only, 2026-10-11 ~09:10 UTC)

### 1.1 Commands run

All read-only. psql only as `claude_ro` (checked: `default_transaction_read_only
= on`, `statement_timeout = 2min`).

| Where | Command |
|---|---|
| server box | `ssh google 'uptime; free -m; df -h /; cd ~/wifi-audit/server && docker compose ps && docker system df && docker compose exec -T db df -h /var/lib/postgresql/data && docker stats --no-stream && git log -1 && git status --short'` |
| server DB | `ssh google 'cd ~/wifi-audit/server && docker compose exec -T db sh -c "psql -X -v ON_ERROR_STOP=1 -U claude_ro -d \"\$POSTGRES_DB\""' < q.sql` with three query files and one single query: `schema_meta`; `sensors`; table sizes (`pg_table_size`, `pg_indexes_size`, `reltuples`); `to_regclass('observations_v5')`; indexes of `observations`; poll and rollup ranges; devices by type; current AP load (`devices.cur_*`); detections; alerts by header and severity; `ap_config_changes` by field; crypt / crypt_bits / MFP combinations of all APs; `ht_mode`; channels; vendors; random / hidden / short-lived APs; which MAC columns the alerts fill (texts printed with MACs replaced by `<MAC>` in the query); SOURCEERROR per day; detection evidence keys; poll health per day; memory settings |
| server files | `ssh google 'ls -la ~/wifi-audit/server/backups/ ~/wifi-audit/server/; cat ~/import.log ~/migrate7.log'` |
| server snapshots | `ssh google 'cd ~/wifi-audit/server && nice python3 - buffer-snapshot.db buffer-snapshot-20261010.db' < snapcheck.py` – a stdlib script that opens each SQLite file with `mode=ro&immutable=1` (no lock, no journal, nothing written) and counts per UTC day the AP observation rows with `n_clients` / `qbss_stations` / `util_pct`. It stopped with a formatting error on the first day without any load value, after the last day that has them. |
| Pi | `ssh pi 'dpkg-query -W "kismet*"'`; `ssh pi 'ls -la /var/lib/wifi-sensor/; df -h /; free -m; systemctl show wifi-sensor-collector.service -p ActiveEnterTimestamp; cd ~/wifi-audit-private && git log -1'` |
| Kismet source | `curl` of `packet_ieee80211.h`, `phy_80211.h/.cc`, `phy_80211_components.h/.cc`, `phy_80211_dissectors.cc`, `alertracker.h` from `raw.githubusercontent.com/kismetwireless/kismet/kismet-2025-09-R1/` into the session scratchpad (read as text) |
| Mac | the stdlib unit suites (last row of 1.2) |

### 1.2 What is there

| Item | Value |
|---|---|
| Schema | `schema_version` **7**, PostgreSQL 17.11, database 972 MB |
| Largest tables | `observations` 718 MB (heap 367 + indexes 351; ~3.47 M rows), `devices` 87 MB (194.5 k), `probes` 37 MB, `client_bssids` 27 MB, `device_freq_hist` 24 MB, `device_config_history` 21 MB (68.6 k), `alerts` 14 MB (9,968 rows), `polls` 11 MB (70,282), `ap_rssi_hourly` 10 MB (41,793), `associations` 9 MB, `ap_config_changes` 4.8 MB (21,607), `ap_summary` 0.6 MB (836), `sensor_hourly` 609 rows, `ap_baselines` 129, `detections` 8 |
| `observations_v5` | **does not exist any more** (dropped after the schema-7 migration). See 1.3 for where its data still is. |
| `sensors` | one row: `pi-fri`, **`tz = Europe/Bratislava` (already set)**, `location` and `description` NULL |
| Data span | polls 2026-09-15 13:53:07 → 2026-10-10 21:03:47 (64,819 of 70,282 ok); `ap_rssi_hourly` up to 2026-10-06 19:00 (adapter loss); last `refresh_rollups` 2026-10-10 21:44 |
| AP activity | 836 AP devices: 436 randomized BSSIDs, 290 hidden (28 with a name seen), 474 with lifetime < 2 h; 378 distinct SSIDs/names. Per hour on average 70.7 active APs (max 108) and 1.4 new ones |
| Current AP load | 791 APs carry `devices.cur_*`; the newest is 2026-10-06 19:33:15 (no AP rows were imported after the adapter loss) |
| Detections | 8 `deauth_flood` (2 high, 2 medium, 4 low), **none acked** |
| Kismet alerts | 9,968: SOURCEERROR 9,550 (Kismet severity 20; 3–10 a day normally, 852–3,954 a day during the adapter loss), NOCLIENTMFP 158 (5), SOURCEOPEN 128 (20), BEACONRATE 64 (15), DEAUTHFLOOD 44 (10), DISCONCODEINVALID 15 (15), BCASTDISCON 6 (10), ADVCRYPTCHANGE 2 (15), CRYPTODROP 1 (15). **NOCLIENTMFP rows carry the client MAC** in `source_mac`, `dest_mac`, `transmitter_mac` and in `text`. |
| Config timeline | `ap_config_changes`: `adv_channel` 21,544 rows on 164 APs, `ht_mode` 53, `mfp_sup` 23, `beacon_rate` 21, `crypt` 6, `crypt_bits` 6, `ssid` 3 |
| HT modes | HT20 460, none 260, HT80 91, HT40+ 21, HT40− 4 |
| Poll health | the adapter loss shows as `ok` polls with `ds_running = false` and 0 devices (10-07: 2,880 polls, 1,745 ok, 1,737 datasource down, 1,745 with 0 devices) – `ok` alone does not mean "collecting" |
| Box | 955 MB RAM, 459 MB available, swap 363 of 4,095 MB used, load 0.23; disk 19 GB, **4.4 GB free** (same filesystem as the DB volume); db/api/frontend 20–24 MB RSS each, plus an unrelated `twitch-drops-miner` container (74 MB); images 1.1 GB (652 MB reclaimable), build cache 134 MB |
| Server checkout | `4492d51`, 6 commits behind `main` (sensor and docs only), clean apart from two log files |
| Pi | Kismet **2025-09-R1** (all packages); `buffer.db` 594 MB (+12 MB WAL); 9.6 GB free; repo at `cf8819f` |
| Tests (Mac, stdlib) | server `tests` 4, `detection` 65, `seed` 9, `api` 7, `evaluation` 26, collector 72, `sensor` 27 – all OK |

### 1.3 Where the pre-v6 AP load history is

| Source (on the server box) | Span | AP rows with `n_clients` / `qbss_stations` / `util_pct` |
|---|---|---|
| `backups/wifi_audit-20261003T103952Z.dump` – pg_dump of schema 6, taken 7 min before the schema-7 migration (`migrate7.log`: 3,412,406 observation rows, 2,492,372 of them AP rows) | 2026-09-15 13:53:07 → 2026-10-01 07:37:31 | not counted (needs a restore); from 09-17 06:57 the same rows as the next line, plus the first ~41 h from the 09-20 seed |
| `server/buffer-snapshot.db` (buffer v5, 557 MB) | 2026-09-17 06:57:47 → 2026-10-01 07:37:31 | 2,182,339 of 2,184,358 AP rows (99.9 %); the three columns are always filled together; `qbss_stations > 0` on 523,000 rows, `util_pct > 0` on 1,983,020; 82–91 APs a day report a BSS Load value > 0 |
| `server/buffer-snapshot-20261010.db` (buffer v6, 429 MB) | polls 2026-09-26 20:25:13 → 10-10 21:03:47; load values up to **2026-10-03 11:00:02** | 1,070,487 AP rows with load values (09-26 → 10-03) |

So the whole load history from 2026-09-15 13:53 to 2026-10-03 11:00:02 is
recoverable from files on the box. The SPEC's "next snapshot export carries
the 10-01 → 10-03 rows" has already happened (the 10-10 snapshot). Two
consequences:

- **Keep these three files.** The 10-03 dump sits in `backups/`, where
  `backup.sh` (KEEP=7) prunes it on the 6th backup from now – Phase 1 moves it
  out of the prune pattern first.
- From 2026-10-03 11:00:20 until collector v7 runs there is no load history
  anywhere (only the latest value per AP). The adapter loss covered
  10-06 19:33 → 10-10 21:53; since then capture runs again, so **every day
  before v7 is deployed is a day of client/utilization history lost.**

---

## 2. Proposed changes to the SPEC (need approval)

| # | Change | Why | Section |
|---|---|---|---|
| D1 | Two schema versions: **8** = AP load history only (phase 1, additive), **9** = the dashboard model (phase 2) | phase 1 needs a server table for the load history before the rest of the model exists; one migration per integer version, as with 7 | 4 |
| D2 | No per-hour *count* rollups for the period cards. Period cards count **distinct** APs/networks per bucket at read time from the AP-hour rows (`ap_rssi_hourly`, `ap_load_hourly`). Daily presence rows are designed for ranges > 30 days (11.2) but not built | hourly counts cannot be combined into a 6-hour or daily bucket: an AP active in 5 of 6 hours counts 5 times with `sum` and too few APs with `max`. 30 days are ~51 k AP-hour rows per sensor | 4.3, 7 |
| D3 | `aps` stores only the **manual** classification; the effective one (manual, else rules) is computed at read time; `classified_by` is derived. `aps.network_id` dropped (a network is its SSID, or `name_seen` for a hidden AP) | rules depend on settings that change; stored rule results would need a recompute on every settings write | 4.3 |
| D4 | `ap_load_hourly` stores sums and counts (exact, mergeable means), the AP's **channel in the key**, the number of polls with a BSS Load element, and a `source` column | campus APs change channel ~17× a day; utilization per channel and hour must follow the AP. Without the element the stored value is 0, not NULL (every v5 AP row has a value, only ~24 % are > 0); Kismet's `dot11e_qbss` flag tells the two apart | 4.2, 9 |
| D5 | `security_level()` follows "weakest offered": WPA2/WPA3 transition (PSK + SAE, MFP optional) = `psk_no_mfp`; TKIP offered next to CCMP = `legacy` | boards show transition mode green; it affects 122 of 836 APs | 5, Q1 |
| D6 | Events are **rows in `detections`** with a `kind` column (detection, kismet, config, sensor); Kismet alerts become episodes per AP and header; channel changes and SOURCEERROR/SOURCEOPEN are not events | one table gives status, keyset paging and filters for free; 21,544 channel changes and 9,550 source errors would bury the feed | 6, Q3 |
| D7 | New endpoints under **`/api/v2/`** while the old ones stay | `/api/overview` and `/api/aps/{key}` exist with other shapes | 7 |
| D8 | AP detail RSSI chart: **hourly** medians from `ap_rssi_hourly` (168 points per sensor) instead of 2-hour medians | 2-hour medians cannot be built from hourly medians, and raw observations over 7 days break the 48 h rule | 7, Q5 |
| D9 | "Successful poll" = `ok AND ds_running AND devices_active > 0` | `ok` alone counted the 4-day adapter loss as ~60 % successful | 4.3 |
| D10 | Kismet alert severity = Kismet's own scale (verified, 6.3) with an override table | the source defines it; overrides stay explicit | 6.3 |

---

## 3. Gap analysis

**have** = in a table the dashboard may read today · **derive** = the data
exists, a new function / rollup / query is needed · **new** = new stored data
(collection, configuration or a write path). "Period" says whether the card
follows the period filter.

### 3.1 Shell (every page)

| Element | Period | Source today | Gap |
|---|---|---|---|
| Sidebar *Sensors*: dot + `pi-fri · online` / `offline 3 h` | now | `sensor_summary.last_poll` | derive: status from the last *capturing* poll vs `account_settings.sensor_offline_min`. Until ingest exists every sensor looks offline between imports (Q7) |
| Sidebar *System*: `API ok · database ok`, `schema vN` | now | `GET /api/health` | have (moves from the top bar into the sidebar, same 60 s re-check) |
| Brand + account name | – | – | new: `accounts` |
| `Latest data: <time>` | now | `sensor_summary.last_poll` | have (max over the account's sensors) |
| `Write locked` / `Write unlocked` | – | – | new: `WRITE_TOKEN`, `POST /api/v2/auth/check` (phase 5; earlier phases always show `Write locked`) |
| Filters in URL + localStorage | – | – | new: `useFilters()` |

### 3.2 Overview `/`

| Card | Period | Source today | Gap |
|---|---|---|---|
| Sensor cards (name, location, last poll, Online, `Channels 89/89`) | now | `sensors`, `sensor_summary`, last poll's `ds_hop_visited/ds_hop_n` | derive: last-poll hop values into `sensor_summary`; `sensors.location` is NULL today (seed) |
| Open events, count by severity | now | `detections` (detector rows only, `acked`) | new: `status`, derived event rows (6) |
| Managed networks compliant `m / n` + first problem | now | – | new: `networks`; derive: `security_level()`, `security_meets()` |
| Needs attention (open high/medium events + compliance findings) | now | – | derive from the two above |
| Header "exact window" | period | – | derive: bucket boundaries in the API (7.2) |
| Tile *New APs* (+ share mobile, sparkline) | period | `sensor_hourly.new_aps` is per sensor | derive: account-level `aps.first_seen`, mobile flag |
| Tile *Open networks nearby* (+ share of APs seen) | period | – | derive: security per AP-hour (`ap_security_periods`) × presence |
| Tile *Clients on managed APs* (peak, average, time of peak) | period | – | **new: `ap_load_hourly`** (collector v7 + backfill), `networks` |
| *Access points over time*: fixed/mobile, event dots, hatched gaps, tooltip with coverage notes | period | `ap_rssi_hourly` (presence), `sensor_hourly` (gaps) | derive: distinct APs per bucket, mobile flag, coverage per sensor and bucket (`capture_polls`, D9), events per bucket |
| *Security of nearby APs*: 100 % bar + counts | period | `devices.crypt*`, `mfp_*` | derive: `security_level()` |
| *Recent events* (5 newest) | now | `detections` | derive: the unified event rows |

### 3.3 Events `/events`

| Element | Source today | Gap |
|---|---|---|
| Feed: detector results | `detections` | new: `status`, `kind`, `updated_at` |
| Feed: Kismet alerts | `alerts` (raw, with client MACs) | derive: episodes per AP and header, redacted summaries (6.2) |
| Feed: security-relevant configuration changes | `ap_config_changes` | derive: `ap_security_periods` → upgrade/downgrade events |
| Feed: sensor offline / online | `polls` | derive: `sensor_periods` → sensor events |
| Filters: status counts, type, managed only, BSSID/SSID search | – | new: status; derive: managed via `networks` + classification |
| Keyset pagination on `(ts, id)`, 50 per page | – | new: API cursor, index `(ts DESC, id DESC)` |
| Expanded row "Why it fired" | `detections.evidence` | derive: per-kind evidence view; the rule event "unknown BSSID with managed SSID" needs `networks` |
| Expanded row "Who saw it" (per-sensor RSSI, first/last seen, clients) | `ap_rssi_hourly`, `ap_summary`, `devices.cur_*` | derive (account AP → `ap_sensors`) |
| Actions Confirm / False positive, "Add to allowed BSSIDs" | – | new: write endpoints (phase 5) |

### 3.4 Networks `/networks`

| Card | Period | Source today | Gap |
|---|---|---|---|
| Rogue banner (unknown BSSID with a managed SSID) | now | – | new: `networks`; derive: classification `suspicious` |
| Networks over time (configured / mobile / other) | period | presence | derive: network key per AP, category, distinct per bucket |
| Network security over time (4 levels) | period | – | derive: weakest level of the network's active BSSIDs per bucket; `unknown` shown as a 5th neutral segment only when > 0 |
| Security changes in the period | period | `ap_config_changes` | derive: level changes from `ap_security_periods`, grouped per network and minute ("+ 3") |
| Managed networks table (policy vs actual, compliance, sensors per network, clients now, BSSID rows with channel/band/HT, security, clients, RSSI by sensor, last seen) | period | `ap_summary`, `devices.cur_*`, `ap_rssi_hourly` | new: `networks`; derive: compliance, per-sensor median RSSI in the period |
| Clients heatmap (day by day / average week, per-network split in the hint) | period | – | **new: `ap_load_hourly`** |
| Friendly networks (BSSIDs, vendors, security, sensors with RSSI, note) | period | – | new: `networks.note` |
| Other networks (+ "Phone hotspots" summary row, presence, quick actions) | period | – | derive: presence class (13, Q9); new: write path for the actions |

### 3.5 Access points `/aps` and AP detail `/aps/:bssid`

| Element | Period | Source today | Gap |
|---|---|---|---|
| Header: APs in the account, active now, new in 24 h | now | `ap_summary` per sensor | derive: account-level `aps` |
| Search, group by none/SSID/sensor, classification chips with counts, band / Open / New only / Hide hotspots | period | – | derive: effective classification, band (`band_of_channel`), security, mobile |
| Table: Status, SSID, BSSID, vendor, classification, security, channel, clients, utilization, last seen | period | `ap_summary` | derive; Status: active = seen within 5 min of its sensor's last capturing poll (Q8) |
| "Seen by sensors (RSSI)" chips, strongest first | period | `ap_rssi_hourly` | derive: median of the hourly medians in the period (Q6) |
| Detail: identity (network link, OUI, channel width, country, first/last seen) | – | `ap_summary` (`ht_mode`: HT20/HT40±/HT80) | derive: account-level first/last seen |
| Detail: security card (encryption + cipher, MFP, rating, policy, recommendation) | – | `crypt*`, `mfp_*` | derive: `security_detail()`, `security_meets()` |
| Detail: current load (clients seen, utilization, stations reported, "measured by pi-fri 30 s ago") | now | `devices.cur_*`, `cur_at` | have; v7 makes "stations reported" NULL when the AP sends no BSS Load element |
| Detail: classification control, note | – | `ap_baselines.note` is per sensor | new: `aps.classification`, `aps.note` (phase 5 writes) |
| Detail: RSSI per sensor, 7 days, baseline band, crosshair | 7 d | `ap_rssi_hourly`, `ap_baselines` | have (D8: hourly medians) |
| Detail: per-sensor statistics (median, robust σ, floor share, observations 7 d, first/last seen) | – | `ap_baselines`, `ap_rssi_hourly`, `ap_summary` | have; the baseline statistics carry their own window (shown as a caption) |
| Detail: clients heatmap (average week, 4 weeks) | 28 d | – | **new: `ap_load_hourly`** |
| Detail: utilization of its channel per 6 h (average, hourly peak) | 7 d | – | **new: `ap_load_hourly`** |
| Detail: configuration history (+ "first captured by pi-lab", "changes channel ~17× a day") | – | `ap_config_changes`, `ap_summary.first_obs` | derive: per-sensor first capture, changes per day |
| Detail: events of this AP | – | `detections` | derive (status + `status_note` shown as "Marked: …") |

### 3.6 Spectrum `/spectrum`

| Card | Period | Source today | Gap |
|---|---|---|---|
| APs per channel (hotspots excluded), tooltip: count, average utilization, DFS/overlap note | period | `devices.adv_channel` (current only) | derive: each AP counted once on its *primary* channel = the channel with most polls in the period (`ap_load_hourly`; before v7/backfill: `ap_channel_at()`); utilization **new** |
| Channel plan issues: overlapping 2.4 GHz, 40 MHz in 2.4 GHz, busiest channel + daytime utilization, channel-change frequency, 1/5/9/13 vs 1/6/11 | period | `ht_mode`, `ap_channel_changes` | derive (definitions in 7.3); utilization **new** |
| Utilization heatmap: *Average day* / *Timeline* (24 × 1 h, 168 × 1 h, 180 × 4 h) | period | – | **new: `ap_load_hourly`** |

### 3.7 Sensors `/sensors`

| Element | Source today | Gap |
|---|---|---|
| Status, "since", last poll (every 30 s) | `sensor_summary`, `polls` | derive (poll interval = median spacing) |
| Hardware | – | new: `sensors.hardware` (seed) |
| Last upload ("after every poll") | – | new: `sensor_uploads` (importer writes one row per import; ingest later) |
| Coverage, last 7 days (% successful polls) | `sensor_hourly` | derive with `capture_polls` (D9) |
| Capture crashes, last 24 h (+ recovery time) | `alerts` SOURCEERROR / SOURCEOPEN | derive: SOURCEOPEN count, gap to the preceding SOURCEERROR |
| Buffer on the sensor (size, days) | – | new: `export_snapshot.py` records the buffer size in the snapshot's `meta`; the importer stores it in `sensor_uploads` |
| APs seen in 7 days, of them only by this sensor | `ap_rssi_hourly` | derive via `ap_sensors` |
| Capture configuration, "confirmed", pending change | `polls.ds_hop_n` (measured) | new: `sensor_config` (seeded from findings §7) |
| Coverage per day, 14 days (successful / limited / outage / not installed) | `sensor_hourly` | derive (+ `limited_polls`); a 5th state "no data yet" after the last imported poll |
| Outages and limited periods (from, duration, type, cause) | `polls`; causes in `detection/degraded_windows.csv` and findings §6/§7 | derive: `sensor_periods`; new: `sensor_annotations` (seeded) |

### 3.8 Settings `/settings/*`

| Tab | Gap |
|---|---|
| Networks (managed/friendly, expected security, MFP required, allowed BSSIDs/OUIs) | new: `networks` + write endpoints |
| Classification (4 rules) | new: `account_settings` |
| Detectors (deauth bursts/window, evil twin k and min dB, guard, sensor-offline minutes) | new: `account_settings`; the batch detectors read them in a later step (today they take CLI options) |
| Sensors (plan, dwell, poll for selected sensors; mixed-plan warning; history with data boundaries) | new: `sensor_config`, `sensor_annotations`; delivery to the sensor is out of scope (Q10) |
| Write-token box | new (phase 5) |

---

## 4. Schema 8 and 9

### 4.1 Overview

| Version | Phase | Content | Migration |
|---|---|---|---|
| 8 | 1 | `ap_load_hourly` (source data, like `observations`), `ap_channel_at()`, `load_ap_load_samples()` | additive, seconds; no lock on existing tables |
| 9 | 2 | accounts, settings, networks, account-level APs, security periods, sensor periods/annotations/config/uploads, event columns on `detections`, new rollup columns, `refresh_rollups()` extended | one transaction + full refresh (~1–2 min) |

Both follow `ops/migrate_schema7.*`: a `.sh` wrapper (version check, disk
estimate, `tee` log), a `.sql` file that is ONE transaction with numbered
`\echo [n/m]` steps and before/after counts, then `schema.sql`, `VACUUM
(ANALYZE)`, `refresh_rollups()` per sensor and the SQL fixture tests. Plus a
read-only `tests/sql/check_schemaN.sql` that Claude runs as `claude_ro`
afterwards. `schema.sql` stays the complete definition at the current version
(a fresh volume is created from it); `tests/test_schema_files.py` keeps the
version and the shared function definitions of migration and `schema.sql`
equal, as for 7.

### 4.2 Schema 8 (phase 1): AP load history

```sql
CREATE TABLE ap_load_hourly (
  sensor_id     integer NOT NULL,
  device_key    text NOT NULL,
  hour          timestamptz NOT NULL,   -- start of the UTC hour
  channel       text NOT NULL,          -- AP's advertised channel at those polls ('' = unknown)
  polls         integer NOT NULL,       -- polls with a new observation of the AP (on this channel)
  clients_polls integer NOT NULL,       -- polls with a Kismet client count
  clients_max   integer,                -- Kismet num_associated_clients
  clients_sum   integer,
  qbss_polls    integer NOT NULL,       -- polls whose beacon carried a BSS Load element
  qbss_max      integer,                -- AP-reported station count
  qbss_sum      integer,
  util_max      real,                   -- AP-reported channel utilization, %
  util_sum      real,
  source        text NOT NULL CHECK (source IN ('buffer', 'v5_obs')),
  PRIMARY KEY (sensor_id, device_key, hour, channel),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);
```

- `source`: `buffer` = hourly rows written by collector v7; `v5_obs` = rebuilt
  from per-poll columns of buffers ≤ v5 (backfill). Before v7 there is no
  BSS-Load presence flag, so for `v5_obs` a poll counts in `qbss_polls` when
  `qbss_stations > 0 OR util_pct > 0` (an AP sending the element with exactly
  0 stations and 0 % in a poll is miscounted as "not reporting"; documented).
- Merge rule everywhere (importer, backfill, later ingest): `ON CONFLICT DO
  UPDATE … WHERE EXCLUDED.polls > ap_load_hourly.polls` – the fuller
  aggregate of the same hour wins. Re-imports and overlapping sources are
  idempotent; a partial current hour is replaced by the complete one later.
- Derived per AP-hour (read time): **clients** = `max(qbss_max)` if any row of
  the hour has `qbss_polls > 0`, else `max(clients_max)`, max across channels
  and sensors (SPEC §4); **utilization** = `sum(util_sum) / sum(qbss_polls)`,
  peak = `max(util_max)`.
- `ap_channel_at(sensor_id, device_key, ts) RETURNS text`: the AP's advertised
  channel valid at `ts` = `adv_channel` of the earliest
  `device_config_history` row with `ts' > ts` and `adv_channel IS NOT NULL`
  (that row holds the configuration replaced at `ts'`), else
  `devices.adv_channel`. This is the `adv_channel` reading of
  `ap_config_changes` (NULL = unknown, no-record states skipped); a fixture
  test proves both agree.
- `load_ap_load_samples(sensor_id, source) RETURNS TABLE (hours, inserted,
  updated)`: aggregates the session temp table `stage_load_samples(ts,
  device_key, n_clients, qbss_stations, util_pct)` into `ap_load_hourly`
  (AP devices only; channel per sample set-based: APs without history rows
  take `devices.adv_channel`, the ~164 with history a lateral lookup on the
  `device_config_history` primary key). Used by the importer and by the
  dump backfill (9.3).
- Fit with `refresh_rollups()`: `ap_load_hourly` is source data and is never
  written by the refresh. Nothing derived is stored from it: heatmaps and
  utilization read it directly (≤ 60 k rows for 30 days). Every load path
  ends with a refresh call, so `sensor_summary.refreshed_at` (the ETag)
  moves.

### 4.3 Schema 9 (phase 2): the dashboard model

New tables (configuration and account level):

```sql
CREATE TABLE accounts (
  id            serial PRIMARY KEY,
  name          text NOT NULL UNIQUE,                 -- 'KIS FRI' (Q11)
  tz            text NOT NULL DEFAULT 'Europe/Bratislava',  -- bucket and day boundaries; whole-hour offsets only
  write_version bigint NOT NULL DEFAULT 0,            -- bumped by every write endpoint (ETags)
  created_at    timestamptz NOT NULL DEFAULT now());
ALTER TABLE sensors ADD COLUMN account_id integer REFERENCES accounts(id);  -- NOT NULL after the backfill
ALTER TABLE sensors ADD COLUMN hardware text;          -- 'Raspberry Pi 3B+ · RTL8821CU (USB)'

CREATE TABLE account_settings (                      -- typed columns, not jsonb: CHECKs + explicit migrations
  account_id          integer PRIMARY KEY REFERENCES accounts(id),
  ignore_random_bssid boolean NOT NULL DEFAULT true,
  suspicious_unallowed boolean NOT NULL DEFAULT true,
  managed_by_vendor   boolean NOT NULL DEFAULT false,
  ignore_weaker_dbm   smallint,                        -- NULL = off
  mobile_vendors      text[] NOT NULL DEFAULT '{}',    -- devices.manuf strings (Q11)
  deauth_min_bursts   integer NOT NULL DEFAULT 3,      -- deauth_flood --min-bursts
  deauth_window_s     integer NOT NULL DEFAULT 600,    -- deauth_flood --burst-window (board shows 300; detector default is 600)
  evil_twin_k         real NOT NULL DEFAULT 6,
  evil_twin_min_db    real NOT NULL DEFAULT 8,
  evil_twin_guard_s   integer NOT NULL DEFAULT 0,      -- off by default (coverage over FP suppression)
  sensor_offline_min  integer NOT NULL DEFAULT 15,
  updated_at          timestamptz NOT NULL DEFAULT now());

CREATE TABLE networks (
  id                bigserial PRIMARY KEY,
  account_id        integer NOT NULL REFERENCES accounts(id),
  ssid              text NOT NULL CHECK (ssid <> ''),
  kind              text NOT NULL CHECK (kind IN ('managed', 'friendly')),
  expected_security text CHECK (expected_security IN ('enterprise', 'psk_or_better', 'wpa3', 'open_allowed')),
  require_mfp       boolean NOT NULL DEFAULT false,
  allowed_bssids    macaddr[] NOT NULL DEFAULT '{}',
  allowed_ouis      macaddr[] NOT NULL DEFAULT '{}', -- trunc()ed (EC:58:EA:00:00:00); the API shows EC:58:EA
  note              text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, ssid),
  CHECK (kind = 'managed' OR (expected_security IS NULL AND NOT require_mfp)));

CREATE TABLE aps (                                   -- one physical AP = one BSSID in the account
  id             bigserial PRIMARY KEY,
  account_id     integer NOT NULL REFERENCES accounts(id),
  bssid          macaddr NOT NULL,
  first_seen     timestamptz NOT NULL,               -- derived: min first observation over its sensors
  last_seen      timestamptz NOT NULL,               -- derived
  classification text CHECK (classification IN ('managed', 'friendly', 'unknown', 'suspicious', 'ignored')),
                                                     -- manual only; NULL = the rules decide (D3)
  classified_at  timestamptz,
  note           text,
  note_at        timestamptz,
  UNIQUE (account_id, bssid));
CREATE TABLE ap_sensors (                            -- derived; rebuilt per sensor by refresh_rollups()
  ap_id      bigint NOT NULL REFERENCES aps(id),
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX ap_sensors_ap ON ap_sensors (ap_id);
```

New derived tables (written only by `refresh_rollups()`):

```sql
CREATE TABLE ap_security_periods (                   -- the security reading of the config timeline
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  valid_from timestamptz NOT NULL,                   -- '-infinity' for the first known state
  valid_to   timestamptz NOT NULL,                   -- 'infinity' for the current state
  crypt text, crypt_bits bigint, mfp_sup boolean, mfp_req boolean,
  level      text NOT NULL,                          -- security_level()
  label      text NOT NULL,                          -- security_label()
  PRIMARY KEY (sensor_id, device_key, valid_from));

CREATE TABLE sensor_periods (                        -- outages and limited capture, from polls
  sensor_id integer NOT NULL REFERENCES sensors(id),
  starts_at timestamptz NOT NULL,
  ends_at   timestamptz,                             -- NULL = still going at the sensor's last poll
  kind      text NOT NULL CHECK (kind IN ('outage', 'limited')),
  polls     integer NOT NULL,
  PRIMARY KEY (sensor_id, starts_at, kind));
```

- `ap_security_periods`: per AP and sensor, change points = the `ts` of
  `ap_config_changes` rows for `crypt`, `crypt_bits`, `mfp_sup`, `mfp_req`;
  each field valid at `t` = `new_value` of its last change ≤ `t`, else
  `old_value` of its first change > `t`, else the current value. The current
  `crypt_bits` uses the last *known* value (rule 3 of `ap_config_changes`:
  0 is unknown unless `crypt = 'Open'`) – the collector's `_counter()` stores
  Kismet's missing-field 0 over a known bitfield. APs without a security
  change (all but ~10) get one period `(-infinity, infinity)`. Feeds: per-hour
  security of the period cards, "Security changes in the period", config
  events.
- `sensor_periods`: each poll is *capturing* (`ok AND ds_running AND
  devices_active > 0`), *limited* (capturing and `ds_hop_ok = false`) or
  *down*; a poll gap > 90 s is down. Runs of down ≥ 90 s → `outage`, runs of
  limited ≥ 60 s → `limited`. Before buffer v4 (`ds_hop_ok` NULL) limited
  windows exist only as annotations.

Configuration and history tables for the Sensors pages (seeded once, written
by the developer or later by the API):

```sql
CREATE TABLE sensor_annotations (                    -- documented causes and changes (findings §6/§7)
  sensor_id integer NOT NULL REFERENCES sensors(id),
  starts_at timestamptz NOT NULL,
  ends_at   timestamptz,                             -- NULL = a point in time (restart, change)
  kind      text NOT NULL CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change')),
  boundary  boolean NOT NULL DEFAULT false,          -- a data boundary (channel-plan change)
  label     text NOT NULL,                           -- short cause shown in the UI
  ref       text,                                    -- e.g. 'findings.md §6'
  PRIMARY KEY (sensor_id, starts_at, kind));

CREATE TABLE sensor_config (                         -- newest row = desired configuration
  sensor_id    integer NOT NULL REFERENCES sensors(id),
  version      integer NOT NULL,
  channel_plan text NOT NULL CHECK (channel_plan IN ('all_89', 'ht20_37', 'no_empty_dfs_59', 'locked', 'custom')),
  channels     text[] NOT NULL,                      -- explicit Kismet hop entries
  dwell_s      real NOT NULL,
  poll_s       integer NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text NOT NULL DEFAULT 'operator',     -- 'seed' for rows taken from findings §7
  confirmed_at timestamptz,                          -- sensor reported it applied (seed: effective time)
  PRIMARY KEY (sensor_id, version));

CREATE TABLE sensor_uploads (                        -- one row per import (later: per ingest batch)
  id             bigserial PRIMARY KEY,
  sensor_id      integer NOT NULL REFERENCES sensors(id),
  received_at    timestamptz NOT NULL DEFAULT now(),
  via            text NOT NULL CHECK (via IN ('snapshot', 'ingest')),
  buffer_version text,
  polls_from     timestamptz, polls_to timestamptz,
  buffer_bytes   bigint,                             -- from the snapshot's meta (export_snapshot.py)
  rows           jsonb NOT NULL DEFAULT '{}');       -- inserted rows per table

CREATE TABLE kismet_alert_rules (                    -- which Kismet alerts are events, severity overrides
  header        text PRIMARY KEY,
  as_event      boolean NOT NULL DEFAULT true,
  severity      text CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),  -- NULL = Kismet's own
  episode_gap_s integer NOT NULL DEFAULT 3600,
  label         text);
-- seed: SOURCEERROR, SOURCEOPEN as_event = false (sensor health, 6.2); labels for the 7 other headers

CREATE TABLE audit_log (                             -- every write endpoint (thesis traceability)
  id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
  account_id integer NOT NULL, action text NOT NULL, target text, old jsonb, new jsonb);
```

Changed tables:

| Table | Change |
|---|---|
| `detections` | `+ kind` (`detection`/`kismet`/`config`/`sensor`, default `detection`), `+ status` (`open`/`confirmed`/`false_positive`, default `open`), `+ ts_end`, `+ updated_at`; `acked_at` → `status_at`, `ack_note` → `status_note`; `status` migrated from `acked` (0 acked rows live); `acked` dropped; `detections_open` becomes `(sensor_id, severity) WHERE status = 'open'`; `detections_dedupe` recreated `NULLS NOT DISTINCT` (sensor events have no subject); `+ detections_feed (ts DESC, id DESC)` |
| `sensor_hourly` | `+ capture_polls` (D9), `+ limited_polls` |
| `sensor_summary` | `+ last_capture_at`, `+ last_hop_n`, `+ last_hop_visited`, `+ last_hop_ok` |
| `ap_summary` | `+ security`, `+ security_label`, `+ crypt_bits_known`, `+ rssi_typical` (median of the hourly medians over the AP's life; used by "ignore weaker than") |
| `ap_rssi_hourly` | `+ index ap_rssi_hourly_hour (sensor_id, hour) INCLUDE (device_key, n_obs, n_valid, median)` – period queries scan by hour, index-only |

Functions and views (in `schema.sql`, all with fixture tests):

| Object | Purpose |
|---|---|
| `security_level(crypt, crypt_bits, mfp_sup, mfp_req)`, `security_label(…)`, `security_detail(crypt_bits) → jsonb` (WPA versions, AKMs, pairwise/group ciphers by name) | section 5 |
| `security_meets(expected_security, require_mfp, crypt_bits, mfp_req) → text` (NULL = met, else the first reason, e.g. `MFP not required`) | compliance |
| `kismet_severity(smallint) → text` | 0 info, 5 low, 10 medium, 15 high, 20 critical (`alertracker.h`, `kis_alert_severity`) |
| view `account_aps` | one row per account AP with the current attributes: SSID / `name_seen` from its newest sensor row, network key (`ssid`, else `name_seen`, else `hidden:<bssid>`), configured network, band, HT, security, mobile flag, effective classification + `classified_by`, sensors, first/last seen. ~850 rows, computed per request (< 20 ms expected; materialize only if measured slow) |
| `ap_classification(...)` | effective classification, in this order: (1) manual; (2) SSID (or `name_seen`) of a **managed** network: BSSID in `allowed_bssids` → managed; OUI in `allowed_ouis` → managed if `managed_by_vendor`, else unknown; neither → suspicious if `suspicious_unallowed` and the network has an allowed list, else unknown; (3) SSID of a **friendly** network → friendly; (4) randomized BSSID and `ignore_random_bssid` → ignored; (5) `rssi_typical < ignore_weaker_dbm` → ignored; (6) unknown. Rule 2 precedes 4 on purpose: the board's evil twin has a randomized BSSID and must be *suspicious*, not ignored |
| `refresh_events(sensor_id)` | derived event rows (6.2); called by `refresh_rollups()` and, for each sensor of the account, by the network/settings write endpoints |

Mobile AP (SPEC §4): `random_bssid OR manuf = ANY(mobile_vendors) OR
(last_seen - first_seen < 2 h AND last_seen < <latest capturing poll> - 1 h)`
– "once it has ended" read as "not seen for an hour".

### 4.4 `refresh_rollups()` in schema 9

Existing steps unchanged (AP hours, sensor hours, `ap_config_changes`,
`ap_summary`, `sensor_summary`); additions, all whole-sensor rebuilds or
idempotent upserts so the invariant "incremental = full" holds:

| Step | What | Cost (est.) |
|---|---|---|
| 3 | `sensor_hourly.capture_polls`, `limited_polls` (same `polls` scan) | ~0 |
| 4b | `ap_security_periods` for the sensor (delete + insert) | < 1 s |
| 5 | `ap_summary` + security columns, `rssi_typical` (one aggregate over `ap_rssi_hourly`) | +1–2 s |
| 5b | `aps` upsert (`account_id`, `bssid`; first/last seen only – never classification/note) and `ap_sensors` rebuild for the sensor | < 1 s |
| 6 | `sensor_summary` + last-poll hop values, `last_capture_at` | ~0 |
| 7 | `sensor_periods` for the sensor (window scan over `polls`, 70 k rows today) | < 1 s |
| 8 | `refresh_events(sensor)` | < 1 s |

Measured base cost: 16 s (AP hours) + 5 s (config view) for a full run on the
12-day seed; a full run now (25 days) is estimated at ~40–60 s, incremental
runs a few seconds. Measured again in phase 2 (`\timing` in the migration log).

### 4.5 Migration and rollback

**Schema 8** (`ops/migrate_schema8.sh` + `.sql`): check version 7, create
`ap_load_hourly` + index, functions, `schema_meta` 8, fixture tests
(`ap_channel_at_test.sql`, `ap_load_test.sql`). No lock on existing tables,
seconds. Rollback (`ops/rollback_schema8.sql`): `DROP TABLE ap_load_hourly`,
drop the two functions, version 7, and check out the previous importer
(the v7 importer needs the table). The backfill is re-loadable from the kept
files; v7 buffer rows can be re-imported for 14 days.

**Schema 9** (`ops/migrate_schema9.sh` + `.sql`), one transaction:

1. checks: version 8; `lock_timeout 10s`; disk estimate (small: new tables
   are < 10 MB at migration time).
2. `accounts` row 1 (name from Q11), `account_settings` defaults,
   `sensors.account_id = 1`, then `NOT NULL`.
3. `networks`, `aps`, `ap_sensors`, `ap_security_periods`, `sensor_periods`,
   `sensor_annotations`, `sensor_config`, `sensor_uploads`,
   `kismet_alert_rules` (+ seed), `audit_log`.
4. `detections`: new columns, status from `acked`, renames, index changes;
   before/after counts per status.
5. `sensor_hourly`, `sensor_summary`, `ap_summary` columns (defaults 0/NULL;
   the full refresh fills them).
6. `ap_rssi_hourly_hour` index.
7. `schema_meta` 9.

Then `schema.sql` (functions, views), `VACUUM (ANALYZE)` of the changed
tables, **full** `refresh_rollups(sensor, NULL)` per sensor (the new hourly
columns need every hour), the fixture tests, and `ops/seed_sensor_history.sql`
(annotations from `detection/degraded_windows.csv` and findings §6/§7, the
`sensor_config` history; idempotent `ON CONFLICT DO NOTHING`, reviewed by the
developer before it runs). Old api routers (`/api/overview`, `/api/detections`)
change from `acked` to `status` in the same commit and keep their response
field names, so the old frontend keeps working.

Rollback (`ops/rollback_schema9.sql`, one transaction): refuse unless version
9; delete `detections` rows with `kind <> 'detection'`; re-add `acked`
(`status <> 'open'`), rename back, restore the old indexes; drop the new
columns, tables, views and functions; version 8; then apply `schema.sql` of
the schema-8 commit (`git show <commit>:server/schema.sql | psql …`) and run
`refresh_rollups()`. Operator data (networks, settings, classifications,
notes, statuses) is lost – the script first `\copy`s those tables to CSV
next to the log. Last resort: restore the pre-migration dump.

### 4.6 Fixture tests (scratch schema, rolled back)

Same pattern as `refresh_rollups_test.sql`: empty copies of the source tables
in a scratch schema first on the search path, the deployed functions run
against them, everything rolled back; the developer runs them (they need
`CREATE SCHEMA`), the migration scripts run them too.

| Test | Proves |
|---|---|
| `security_level_test.sql` | every combination seen live (5.3) plus synthetic WEP40/104, WPA1-TKIP, WPA2 TKIP-only, TKIP+CCMP, WPA1+WPA2, WPA2-EAP(-FT), WPA3-Enterprise (suite B), WPA3-SAE (MFP required), transition PSK+SAE, PSK with MFP required, PSK-SHA256, FT-PSK, OWE, the OWE-transition quirk, bits 0 with a non-Open string (fallback), NULL → expected level and label |
| `security_meets_test.sql` | 4 policies × levels × MFP; the board's case "Enterprise, MFP only capable" fails `require_mfp` |
| `ap_channel_at_test.sql` | `ap_channel_at()` equals the `adv_channel` reading of `ap_config_changes` on churn fixtures (A → incomplete → A, channel change, no-record states) |
| `ap_load_test.sql` | `load_ap_load_samples()`: aggregation per (AP, hour, channel), channel split inside an hour, BSS Load absent, non-AP rows excluded, fuller-wins merge, re-load idempotent |
| `refresh_rollups_test.sql` (extended) | `capture_polls`/`limited_polls` incl. an "ok but no devices" stretch; a WPA2 → Open downgrade = 2 security periods + one high config event; `aps`/`ap_sensors` (manual classification and note survive a refresh; an AP re-classified bridged leaves `ap_sensors`); `sensor_periods` (gap, no frames, limited); derived events keep their status across refreshes; incremental = full; idempotent |
| `classification_test.sql` | rule order (randomized BSSID with a managed SSID → suspicious; manual wins; allowed OUI with/without the vendor rule; friendly; weaker than X; hidden AP matched by `name_seen`) |
| `events_test.sql` | Kismet alert episodes (gap), SOURCE* not events, no client MAC in any derived row (summary, evidence, `mac`), sensor events from periods ≥ `sensor_offline_min`, config events with level before/after |
| `check_schema8.sql`, `check_schema9.sql` | read-only checks on the real data (as `claude_ro`): load hours vs capturing hours per day; every AP device has an `ap_sensors` row; `aps` = distinct BSSIDs; status counts; sums of the new hourly columns = poll counts |

### 4.7 Size estimates (e2-micro, per sensor)

| Object | Rows | Size |
|---|---|---|
| `ap_load_hourly` | ~80 a hour (70.7 APs + channel splits) ≈ 1.9 k/day; backfill ~34 k (18 days) | ~0.4 MB/day incl. 2 indexes ≈ 150 MB/year; backfill ~7 MB |
| `ap_rssi_hourly_hour` index | 41.8 k now | ~2 MB now, ~35 MB/year |
| `aps` + `ap_sensors` | 836 now, +~34/day (1.4 new APs/h) | ~3 MB/year |
| `ap_security_periods` | ~850 | < 0.5 MB |
| `detections` (all kinds) | ~150 initially (6.4), +5–20/day | < 5 MB/year |
| other new tables | tens of rows | KB |
| **for comparison: `observations`** | ~175 k AP rows/day | **~30–35 MB/day ≈ 12 GB/year** – the real disk limit (risk R1) |

Memory: every new statement runs inside the existing `work_mem` settings
(32 MB in `refresh_rollups`, 64 MB `SET LOCAL` in the backfill); period
queries aggregate ≤ 60 k rows. RAM is not the constraint; disk is.

---

## 5. `security_level()`

### 5.1 Source (verified, Kismet 2025-09-R1 – the version on the Pi)

- `dot11.advertisedssid.crypt_bitfield` – the field the collector stores as
  `crypt_bits` – is registered on `crypt_set`
  (`phy_80211_components.cc:273`), the **64-bit `dot11_crypt_*` set** defined
  in `packet_ieee80211.h:216-289`. The legacy 32-bit `crypt_*` set is a
  different field (`dot11.advertisedssid.crypt_set`, "legacy bitfield",
  `:274`), not stored.
- Bits are set by the dissector (`phy_80211_dissectors.cc`): RSN IE 48 at
  `:2036-2055` via `wpa_rsn_group_conv`, `wpa_rsn_pairwise_conv`,
  `wpa_rsn_auth_conv` (`:231-450`); the WPA vendor IE at `:2346-2367` (sets
  `general_wpa1/2/3` from the IE's version); the privacy bit → WEP (`:618`,
  `:981`).
- MFP: `wpa_mfp_supported` / `wpa_mfp_required` from the RSN capabilities of
  the last beacon, `false` when there is no RSN IE (`phy_80211.cc:2933-2938`).
- `crypt_string` is `crypt_to_simple_string()` (`phy_80211.cc:4119`):
  `Open` only when the whole set is 0; it labels OWE as `-SAE`, so levels are
  computed from the bits, never from the string (except the fallback below).
- Quirk: the OWE-transition vendor IE ORs the **legacy** constant
  `crypt_wpa_owe` (`1 << 17`) into the new set (`phy_80211_dissectors.cc:2383`),
  where bit 17 means "group cipher BIP-CMAC-256". An open BSS in OWE
  transition therefore shows bit 17 without any WPA bit.

Bits used (`packet_ieee80211.h`):

| Group | Bits |
|---|---|
| general | 0 WEP · 1 WPA · 2 WPA1 · 3 WPA2 · 4 WPA3 |
| group cipher | 7 WEP40 · 8 WEP104 · 9 TKIP · 10 CCMP128 · 11 BIP-CMAC128 · 12 GCMP128 · 13 GCMP256 · 14 CCMP256 · 15/16 BIP-GMAC · 17 BIP-CMAC256 · 18 OCB |
| pairwise cipher | 23 WEP40 · 24 WEP104 · 25 TKIP · 26 CCMP128 · 27 BIP-CMAC128 · 28 GCMP128 · 29 GCMP256 · 30 CCMP256 · 31–33 BIP · 34 OCB |
| AKM | 37 802.1X · 38 PSK · 39 802.1X-FT · 40 PSK-FT · 41 802.1X-SHA256 · 42 PSK-SHA256 · 43 TDLS · 44 SAE · 45 SAE-FT · 46 AP-peer · 47/48 802.1X suite B · 49 802.1X-FT-SHA384 · 50–53 FILS · 54 OWE · 55 PSK-FT-SHA384 · 56 PSK-SHA384 · 57 WPS |
| EAP (from data frames) | 58 LEAP · 59 TLS · 60 TTLS · 61 PEAP · 62 identity |

### 5.2 Mapping (first match wins)

| # | Condition | Level |
|---|---|---|
| 1 | `crypt_bits` NULL, or 0 while `crypt` is not `Open` (Kismet's missing field; the rollup passes the last *known* bits first) | fallback: classify the `crypt` tokens the same way (`WEP` → legacy; `WPA1`/`TKIP` without `AES` → legacy; `-PSK` without MFP required → psk_no_mfp; `-EAP`, `-SAE` → strong); no tokens → **unknown** |
| 2 | no WEP bit (0, 7, 8, 23, 24), no WPA general bit (1–4) and no AKM bit (37–57) – covers `Open` with 0 and the OWE-transition quirk (bit 17 alone) | **open** |
| 3 | any WEP bit | **legacy** |
| 4 | WPA1 (bit 2) | **legacy** (the AP offers WPA1, also in WPA1/WPA2 mixed mode) |
| 5 | pairwise TKIP (25) – D5: *offered* TKIP; the literal SPEC reading is "TKIP without CCMP/GCMP" (Q2) | **legacy** |
| 6 | any PSK AKM (38, 40, 42, 55, 56) and `mfp_req` not true – includes WPA2/WPA3 transition (Q1) | **psk_no_mfp** |
| 7 | any 802.1X AKM (37, 39, 41, 47–53), SAE (44, 45), OWE (54), or PSK with `mfp_req` | **strong** |
| 8 | otherwise (WPA bit without a known AKM: TDLS, AP-peer, data-frame evidence only) | **unknown** |

Order of weakness for "network security" (weakest active BSSID per bucket):
open < legacy < psk_no_mfp < strong; `unknown` counts only when no BSSID of
the network has a known level.

`security_label()` (shown in tables): `Open`, `OWE`, `WEP`, `WPA1/WPA2 mixed`,
`WPA2 (TKIP)`, `WPA2-PSK`, `WPA2-PSK, MFP required`, `WPA2/WPA3 transition`,
`WPA3-SAE`, `WPA2-Enterprise`, `WPA3-Enterprise`, `Unknown`. The MFP state
(`required` / `capable` / `no`) is a separate field; ciphers come from
`security_detail()` ("WPA2-Enterprise (802.1X) · CCMP").

`security_meets(policy, require_mfp, bits, mfp_req)`: `enterprise` = an
802.1X AKM and no PSK AKM; `psk_or_better` = level psk_no_mfp or strong;
`wpa3` = SAE / OWE / suite-B 802.1X and no plain PSK AKM (a transition AP
does not meet WPA3); `open_allowed` = anything; `require_mfp` additionally
needs `mfp_req` (ignored for Open).

### 5.3 The live combinations (836 APs, 2026-10-11)

| crypt (Kismet string) | crypt_bits (hex) | MFP sup/req | APs | Decoded | Level (D5) | Literal SPEC |
|---|---|---|---|---|---|---|
| WPA2 WPA2-PSK AES-CCMP | 400400040a | f/f | 335 | WPA, WPA2, group+pairwise CCMP, PSK | psk_no_mfp | same |
| (empty) | 0 | f/f | 172 | no beacon record | unknown | same |
| WPA3 WPA3-PSK WPA3-SAE AES-CCMP | 10400400041a | t/f | 117 | + WPA3, SAE: transition mode | **psk_no_mfp** | strong |
| Open | 0 | f/f | 84 | – | open | same |
| WPA2 WPA2-PSK AES-CCMP | 400400040a | t/f | 52 | PSK, MFP capable | psk_no_mfp | same |
| WPA2 WPA2-PSK TKIP AES-CCMP | 400600020e | f/f | 26 | WPA1+WPA2, group TKIP, pairwise TKIP+CCMP | legacy | same (WPA1) |
| WPA2 WPA2-EAP WPA2-EAP-FT AES-CCMP | a00400040a | f/f | 25 | 802.1X + 802.1X-FT | strong | same |
| WPA2 WPA2-PSK AES-CCMP | 0 | f/f | 9 | bits unknown → string | psk_no_mfp | same |
| WPA2 WPA2-PSK AES-CCMP | 400400040e | f/f | 8 | WPA1+WPA2, CCMP | legacy | same (WPA1) |
| WPA3 WPA3-PSK WPA3-PSK WPA3-SAE AES-CCMP | 14400400041a | t/f | 4 | transition, PSK + PSK-SHA256 + SAE | **psk_no_mfp** | strong |
| WPA2 WPA2-PSK TKIP AES-CCMP | 400600020a | f/f | 2 | WPA2 only, pairwise TKIP+CCMP | **legacy** | psk_no_mfp |
| WPA3 WPA3-PSK WPA3-SAE AES-CCMP | 0 | f/f | 1 | bits unknown → string | **psk_no_mfp** | strong |
| WPA2 WPA2-PSK TKIP AES-CCMP | 400400020e | f/f | 1 | WPA1+WPA2, group TKIP, pairwise CCMP | legacy | same |

D5 moves 122 APs from strong to psk_no_mfp and 2 from psk_no_mfp to legacy.

---

## 6. Events model

### 6.1 Recommendation: rows in `detections` (not a UNION view)

| | Rows in `detections` (`kind`) – **recommended** | UNION view + status side table |
|---|---|---|
| Status for every kind (boards: Confirm on sensor and config events) | one column | a side table keyed by (kind, source ref); "open" = no row → an anti-join on every list |
| Keyset paging `(ts, id)`, filters, counts per status | one index, one query | a Merge Append over 4 sources with per-branch joins and a synthetic id |
| Volume | ~150 rows now (episodes) | ~10 k Kismet rows to filter at read time |
| Cost | `refresh_events()` derives rows at refresh time | none at refresh, more at read |
| Risk | derived rows must never overwrite status | status keyed to a source row that can change (episodes grow) |

`detections` stays the table of record (the evaluation harness reads it by
`type`); detector rows keep `kind = 'detection'` and the existing sink
(`detection/detections.py`) only changes `acked` → `status` in its SELECT and
sets `updated_at`.

### 6.2 What becomes an event

| Kind | Rows | Subject | Severity | Status preservation |
|---|---|---|---|---|
| `detection` | detector rows (deauth_flood, evil_twin) as today; plus the rule event **`unknown_bssid_managed_ssid`** (an AP whose effective classification is suspicious by rule 2 of 4.3) | device_key | detector's / rule: high | sink matches episodes (unchanged) |
| `kismet` | one row per **episode** = alerts of one header on one AP with gaps ≤ `episode_gap_s` (default 1 h); `ts` first, `ts_end` last alert; evidence: header, count, Kismet severity, channel | the AP's device_key when the alert's device is an AP; otherwise **no subject** – never a client MAC | `kismet_alert_rules.severity`, else `kismet_severity()` | matched to the stored row of the same sensor/type/subject whose window overlaps (as the detector sink does); status, `status_at`, `status_note` never touched; a stored row without a match is kept |
| `config` | one row per security change point of `ap_security_periods` | device_key | down to open: high · other downgrade: medium · same level: low · upgrade: info | natural key (sensor, type, subject, ts) |
| `sensor` | one row per `sensor_periods` row ≥ `sensor_offline_min` (`sensor_outage`) or ≥ 10 min (`sensor_limited`); `ts_end` NULL while ongoing | none (sensor only) | outage medium, limited low | natural key; `ts_end` updated |

Not events: **SOURCEERROR / SOURCEOPEN** (sensor health – they feed the
capture-crash count and `sensor_periods` instead); **channel changes**
(21,544 in 25 days, dynamic channel selection on 164 APs – shown in the AP's
configuration history, counted in Spectrum; Q3).

Privacy: the summary of a Kismet event is built from a template per header
(`kismet_alert_rules.label`) with the AP's BSSID/SSID; Kismet's `text`,
`raw` and the alert MAC columns are never copied into `detections` and never
returned by the API. `events_test.sql` asserts it; the API has a response
check in its tests (7.6).

Multi-sensor: one row per sensor (the evidence is per sensor). With one
sensor this is invisible; merging the rows of several sensors into one feed
item ("pi-fri, pi-lab") is a later step (12, R7).

### 6.3 Kismet severity (verified)

`alertracker.h:50-56`: `kis_alert_severity { info = 0, low = 5, medium = 10,
high = 15, critical = 20 }`. Live: NOCLIENTMFP 5, DEAUTHFLOOD 10, BCASTDISCON
10, BEACONRATE 15, DISCONCODEINVALID 15, ADVCRYPTCHANGE 15, CRYPTODROP 15,
SOURCEERROR/SOURCEOPEN 20. With Kismet's scale, BEACONRATE (64 alerts, beacon
rate changes of campus APs) and DISCONCODEINVALID land in "Needs attention"
as high (Q4).

### 6.4 Initial volume

~290 non-source Kismet alerts → an estimated 80–120 episodes; ~30 security
change points; ~15 sensor outages; 8 detections. All start `open` (Q4).

---

## 7. API

### 7.1 Conventions

- New endpoints under `/api/v2/` (D7); `/api/health` stays shared. JSON,
  timestamps ISO 8601 UTC, the account's `tz` in every period response; the
  frontend formats in that zone.
- Filters: `sensor=all|<name>` (default `all`), `range=24h|7d|30d` (default
  `7d`); later `from`/`to` (11.2). Unknown values → 422.
- The account comes from one place: `deps.get_account()` returns account 1
  today; login replaces it later (11.4).
- Caching: weak ETag `W/"v2-<account>-<max refreshed_at of the account's
  sensors, µs>-<accounts.write_version>-<max detections.updated_at>-<bucket
  epoch>"`; the bucket epoch (current hour / 6-hour window / day in the
  account's zone) is part of the tag, otherwise a 304 would keep yesterday's
  bucket boundaries. `Cache-Control: public, max-age=60` as today. Status
  endpoints (`/sensors`) put the current minute into the tag.
- No client MAC, probe SSID or per-client row in any response; `associations`,
  `probes`, `client_bssids` and `alerts.raw/text` are not read by v2.
- Read endpoints public (until login); writes need `Authorization: Bearer
  <WRITE_TOKEN>` (7.5).

### 7.2 Periods and buckets

`api/app/periods.py` (stdlib `zoneinfo`, unit-tested): `24h` = 24 hourly
buckets from the current hour − 23; `7d` = 28 × 6 h aligned to local
00/06/12/18, the last one containing now; `30d` = 30 local calendar days,
today partial. Boundaries are computed in local wall time and converted to
UTC, so DST days give 5/7-hour and 23/25-hour buckets (the 30-day range
crosses 2026-10-25). Bucket edges fall on UTC hours (whole-hour offsets), so
SQL joins hourly rows with `hour >= start AND hour < end` on
`unnest(starts, ends)`.

Common envelope of period endpoints:

```jsonc
{
  "account": {"name": "KIS FRI", "tz": "Europe/Bratislava"},
  "filters": {"sensor": "all", "range": "7d"},
  "period": {"from": "2026-10-03T16:00:00Z", "to": "2026-10-10T15:52:00Z", "bucket": "6h",
             "label": "last 7 days: 3 Oct 18:00 – 10 Oct 17:52"},
  "buckets": [{"start": "…Z", "end": "…Z", "coverage": {"pi-fri": 1.0, "pi-lab": 0.42}}],
  // series: arrays aligned with buckets; null = no selected sensor collecting
}
```

Coverage of a sensor in a bucket = capturing seconds / bucket seconds, from
`sensor_hourly` (`(3600 − gap_s) × capture_polls / polls` per hour). Series
built on `ap_load_hourly` additionally return `load_missing: true` for buckets
where the sensor captured but no load rows exist (2026-10-03 → v7 deploy),
rendered hatched with "client counts were not recorded".

### 7.3 Read endpoints (phase 3)

| Endpoint | Parameters | Response (main fields) |
|---|---|---|
| `GET /api/v2/sensors` | – | `{account, latest_data, write: {enabled}, sensors: [{name, location, hardware, status: online/offline/no_data, last_poll, last_capture_at, offline_s, hop: {n, visited, ok}, poll_s, last_upload: {at, via, buffer_bytes, polls_to}, coverage_7d, crashes_24h: {count, max_recovery_s}, aps_7d, aps_only_here_7d, config: {plan, channels, dwell_s, poll_s, confirmed_at}, pending: {…} \| null}]}` – sidebar, top bar, Sensors cards, Overview sensor cards |
| `GET /api/v2/overview/status` | `sensor` | `{open_events: {total, by_severity}, compliance: {met, total, first_problem: {network, reason}}, attention: [{severity, kind, title, bssid?, network?, sensors, ts, link}]}` |
| `GET /api/v2/overview/period` | `sensor, range` | envelope + `tiles: {new_aps: {total, mobile_share, series}, open_nearby: {total, aps_seen, series}, clients_managed: {peak, peak_at, avg_label, avg, series}}`, `aps: {fixed: [], mobile: [], events: [[{severity, title}]]}`, `security: [{level, label, aps, share}]` |
| `GET /api/v2/events` | `sensor, status (csv), type (detection/kismet/config/sensor, csv), managed (0/1), q, before=<ts>,<id>, after=<ts>,<id>, limit ≤ 100 (50)` | `{counts: {open, confirmed, false_positive, total}, items: [{id, ts, ts_end, severity, kind, type, type_label, summary, ap: {bssid, ssid, network, classification} \| null, sensors: [], status, status_at}], newer, older}` – keyset on `(ts, id)`, never OFFSET; `newer`/`older` are opaque cursors |
| `GET /api/v2/events/{id}` | – | item + `evidence` (per kind, redacted) + `who_saw_it: [{sensor, seen, rssi_median, first_seen, last_seen}]` + `clients` |
| `GET /api/v2/networks` | `sensor, range` | `{rogue: [{ssid, bssid, security, sensors, since, event_id}], managed: [{id, ssid, bssids, bssids_active, vendors, policy, actual, compliance: {met, failing, total, reason}, sensors: {name: n}, clients_now, bssid_rows: [{bssid, channel, band, ht_mode, security, label, clients, rssi: {sensor: dBm}, last_seen}]}], friendly: [{…, note}], other: {networks, hidden, hotspots: {bssids, avg_presence_s}, rows: [{network, bssids, vendors, security, presence}]}, security_changes: [{ts, kind, network, bssids, from, to}]}` – paged `other.rows` (`other_page`) |
| `GET /api/v2/networks/timeseries` | `sensor, range` | envelope + `categories: {configured, mobile, other}`, `security: {open, legacy, psk_no_mfp, strong, unknown}` |
| `GET /api/v2/clients/heatmap` | `sensor, range, view=days\|week` | `{view, rows: [{label, cells: [{start, value, state: ok\|no_data\|outside\|load_missing, by_network: {ssid: n}}]}], peak: {value, at}, ramp}`; `week` → 422 for `24h` |
| `GET /api/v2/aps` | `sensor, range, q, class, band, open, new, hide_hotspots, group=none\|ssid\|sensor, sort, page, page_size ≤ 100 (50)` | `{counts: {all, managed, friendly, unknown, suspicious, ignored, band_2_4, band_5, open, new, hotspots}, total, page, items: [{bssid, ssid, hidden, manuf, classification, classified_by, security, label, policy_met, channel, band, clients, util, seen_by: [{sensor, rssi_median}], last_seen, active, mobile}], groups?}` – offset pages are fine here (sorted, small, not a feed) |
| `GET /api/v2/aps/{bssid}` | – | `{identity, security: {level, label, detail, mfp, policy: {network, met, reason}}, load: {clients, qbss_stations, util, at, sensor}, classification: {value, by}, sensors: [{name, baseline: {median, robust_sd, floor_share, window}, obs_7d, first_seen, last_seen}], config_history: [{ts, changes}], changes_per_day, first_capture: [{sensor, ts}], events: [{…, status, status_note}], note}` |
| `GET /api/v2/aps/{bssid}/rssi` | `days=7` (≤ 30) | `{t: [], series: {sensor: [median \| null]}, baseline: {sensor: {median, robust_sd}}}` hourly (D8) |
| `GET /api/v2/aps/{bssid}/load` | `view=util&range=7d` or `view=clients_week&weeks=4` | util: envelope + `{avg: [], peak: []}`; clients_week: 7 × 24 cells `{value, days}` |
| `GET /api/v2/spectrum` | `sensor, range, band=2.4\|5` | `{channels: [{channel, aps, util_avg, dfs, overlaps}], issues: {overlapping, wide_2g, busiest: {channel, aps, day_util: [p25, p75]}, channel_changes: [{network, per_day: [p25, p75]}], plan: {ch_1_6_11, ch_5_9_13}}}` |
| `GET /api/v2/spectrum/utilization` | `sensor, range, band, view=day\|timeline` | rows per channel (of the band, with ≥ 1 reporting AP) with cells `{start, avg, peak, aps}`; timeline 24 × 1 h / 168 × 1 h / 180 × 4 h |
| `GET /api/v2/sensors/{name}/coverage` | `days=14` | `{days: [{date, ok, limited, outage, not_installed, no_data}]}` shares of the local day |
| `GET /api/v2/sensors/{name}/outages` | – | `[{from, to, duration_s, ongoing, type: outage\|limited\|restart, cause}]` (periods ∪ annotations) |
| `GET /api/v2/settings/{networks\|classification\|detectors\|sensors}` | – | the stored rows; `sensors` adds the history with `boundary` flags |

Definitions the endpoints implement (open to change in review):
*primary channel* = most polls in the period; *overlapping 2.4 GHz* = primary
channel ∉ {1, 6, 11}; *40 MHz in 2.4 GHz* = `ht_mode` HT40±; *busiest* = most
APs, daytime utilization = p25–p75 of its hourly values 08:00–18:00 local;
*channel-change frequency* = changes per AP per observed day, p25–p75 per
network, networks with a median ≥ 4/day; spectrum counts exclude mobile APs.

Kept until the frontend switch (then removed, phase 5): `/api/overview`,
`/api/aps`, `/api/aps/{device_key}`, `/api/aps/{device_key}/rssi`,
`/api/alerts`, `/api/detections`, `/api/detections/{id}`. `/api/findings`
(thesis figures) and `/api/health` stay.

### 7.4 Performance targets

Measured with `tests/perf/measure.sh` (extended with `ep_v2_*.sql`):
warm < 100 ms SQL for every endpoint at `30d`, cold < 1 s. The heaviest are
the 30-day presence aggregates (~51 k rows per sensor through the new
index-only `ap_rssi_hourly_hour`) and `/aps` (850 rows of `account_aps` +
period chips for one page). Large JSON lists are built by PostgreSQL
(`json_agg`) as in the current `/api/aps`.

### 7.5 Write endpoints (phase 5)

| Endpoint | Body | Effect |
|---|---|---|
| `POST /api/v2/auth/check` | – | 204 / 401 (the Unlock button) |
| `POST /api/v2/events/{id}/status` | `{status, note?}` | status, `status_at`, `status_note` |
| `PUT /api/v2/aps/{bssid}/classification` | `{classification \| null}` | manual classification (null = back to the rules) |
| `PUT /api/v2/aps/{bssid}/note` | `{note}` | |
| `POST /api/v2/settings/networks`, `PUT …/{id}`, `DELETE …/{id}` | network fields | + `refresh_events()` per sensor of the account |
| `POST /api/v2/settings/networks/{id}/allowed` | `{bssid \| oui}` | "Add to allowed BSSIDs" |
| `PUT /api/v2/settings/classification`, `PUT /api/v2/settings/detectors` | the typed columns | + `refresh_events()` per sensor of the account |
| `POST /api/v2/settings/sensors` | `{sensors: [names], config: {channel_plan, channels, dwell_s, poll_s}}` | one new `sensor_config` version per sensor; the hop-list length is checked with the installer's stride rule (`hop_stride_ok`, ported and tested against 89 ok / 90 bad) |

Every write: one transaction, `accounts.write_version + 1`, an `audit_log`
row. Token: `WRITE_TOKEN` from `.env` passed to the api container; empty =
writes disabled (403); comparison with `hmac.compare_digest`; 401 without it;
failed attempts logged. Optional least privilege: an `api_rw` role with
SELECT on everything and DML only on the write tables (column grants on
`aps.classification/note`, `detections.status*`), `ops/create_api_role.sql`.

### 7.6 API tests

Stdlib only, so they run on the Mac and in the api image
(`docker compose run --rm api python -m unittest discover -s tests`):
`test_periods.py` (bucket edges for all ranges, DST end 2026-10-25 and start
2027-03-28, partial last bucket), `test_cursor.py` (keyset cursor round
trip, tampering → 422), `test_write_token.py` (missing / wrong / disabled),
`test_etag.py` (bucket epoch in the tag), `test_privacy.py` (a response
scanner: every MAC-shaped string in sample responses must be an AP BSSID).
The SQL behind the endpoints is covered by the fixture tests (4.6) and the
perf scripts run against the real data.

---

## 8. Frontend

### 8.1 Routes

| Route | Page | Replaces |
|---|---|---|
| `/` | Overview | old Overview |
| `/events` | Events | `/detections` (redirect) |
| `/networks` | Networks | – |
| `/aps` | Access points | old Inventory |
| `/aps/:bssid` | AP detail | `/aps/:deviceKey` (old links: a key without colons is looked up and redirected) |
| `/spectrum` | Spectrum | – |
| `/sensors` | Sensors | – |
| `/settings` → `/settings/networks`; `/settings/classification`, `/settings/detectors`, `/settings/sensors?sensor=` | Settings, one tab per route | – |

Pages are switched one by one (phase 4): until a page is rebuilt, its old
version renders inside the new shell and keeps calling the old endpoints.

### 8.2 `useFilters()` (SPEC §3)

```js
const { sensor, range, setFilter, linkSearch } = useFilters();
```

- Source of truth: the URL query (`useSearchParams`). Missing keys are filled
  from `localStorage["wifi-audit.filters.v1"]`, then defaults (`all`, `7d`),
  and written back with `replace` so the URL is always complete and shareable.
- Validation: `range` against the three presets; `sensor` against the names
  from `/api/v2/sensors` (unknown → `all`, replace).
- `setFilter` pushes a history entry and updates localStorage (wrapped in
  try/catch).
- `linkSearch()` returns `?sensor=…&range=…` – only these global keys;
  page-local parameters (`view`, `band`, `page`, `status`) are not carried.
  The sidebar uses a `FilterNavLink`.
- Which pickers the top bar shows comes from the route's `handle: {filters:
  ["sensor", "range"]}` (Events `["sensor"]`, Settings and AP detail `[]`).

### 8.3 Data layer: one hook, per-card keys

`src/data/query.js` (~150 lines, no library):

```js
const { data, error, status, isFetching } = useQuery(["overview.period", sensor, range],
                                                     `/api/v2/overview/period?…`);
```

- One cache entry per key (Map; `useSyncExternalStore`), in-flight
  de-duplication, previous data kept while a new key loads (no flashing
  charts), per-card error and skeleton, so slow cards never block fast ones.
- Requests use `cache: "no-store"` plus the stored ETag in `If-None-Match`; a
  304 keeps the cached data.
- `invalidate(prefix)` – used after writes (e.g. a classification change
  invalidates `aps.*`, `networks.*`, `overview.*`) and, in the live phase, by
  the SSE handler (11.3). `refetchInterval` with pause while
  `document.hidden` is the live-update step 1, off by default.
- Keys carry the sensor scope, so a later `data_changed <sensor_id>` maps to
  the keys to invalidate.

### 8.4 Chart primitives (SVG/HTML, no chart library; uPlot only for RSSI)

| Component | Behaviour (from the boards) |
|---|---|
| `StackedColumns` | series in fixed color order, 2 px gaps between segments, the whole column is the hit target, light wash on hover, tooltip beside the column (never over it) listing every series value-first + total + notes (coverage, events), keyboard focus with ←/→, `aria-label` per column, hatched columns for `null`, event dots, y max rounded (50s), x labels at day / 6-hour marks from the API's bucket starts |
| `Heatmap` | HTML grid, sequential blue ramp with the board thresholds, hovered cell outlined, its text in the hint line above (`aria-live="polite"`), hatched cells for no data / outside / load missing, roving keyboard focus |
| `Sparkline` | the tiles' small columns with the same tooltip |
| `SegmentBar` | the 100 % security bar; hovering a segment highlights its legend row and vice versa |
| `RssiLines` | uPlot (as today's `RssiChart`): one line per sensor (categorical colors), baseline band (median ± robust σ) of the selected sensor, crosshair snapping to the nearest hour, tooltip with every sensor's value, table fallback |
| `Tooltip`, `Legend`, `Hatch` | shared; colors and ramp as tokens in one CSS file |

Formatting (`format.js`, extended): dates `10 Oct 17:52`, weekday `Sat 10
Oct`, 24-hour clock in the account's zone (`Intl.DateTimeFormat` with
`timeZone`, assembled from `formatToParts`), minus sign `−` for dBm.

### 8.5 File layout

```
src/
  main.jsx                     routes (8.1)
  app/       Shell.jsx  Sidebar.jsx  TopBar.jsx  WriteBadge.jsx  FilterNavLink.jsx
  data/      api.js (fetch, ETag, auth header)  query.js  keys.js
  hooks/     useFilters.js  useWriteToken.js (sessionStorage)
  charts/    StackedColumns.jsx  Heatmap.jsx  Sparkline.jsx  SegmentBar.jsx  RssiLines.jsx
             Tooltip.jsx  Legend.jsx  geometry.js (tooltip side, nice max, label ticks)
  components/ Card  Badge  StatusDot  Chip  Segmented  DataTable  Pager  KeysetPager  Skeleton  ErrorBox
  pages/     overview/  events/  networks/  aps/ (ApList, ApDetail)  spectrum/  sensors/
             settings/ (Networks, Classification, Detectors, Sensors, WriteTokenBox)
  styles/    tokens.css  base.css  charts.css
  format.js  security.js (level → color, label)
  legacy/    old pages until their replacement lands (deleted in phase 5)
```

Tests: pure logic stays in `.js` modules (formatting, filter parsing, cache,
chart geometry) and runs with Node's built-in runner (`node --test`, no new
dependency) in the frontend image's build stage, so a failing test fails the
build. No new runtime dependencies are planned (React 19, react-router 7,
uPlot stay).

---

## 9. Collector buffer v7, importer, backfill

### 9.1 Collector buffer v7 (`wifi-sensor/`, follows `wifi-sensor/CLAUDE.md`)

- `shape.py`: request `dot11.advertisedssid.dot11e_qbss` ("SSID advertises
  802.11e QBSS", `phy_80211_components.cc:329`) as `qbss`; when it is 0,
  `qbss_stations` and `util_pct` become None (today Kismet's 0 is stored as a
  real 0). This also changes `devices.cur_qbss_stations` / `cur_util_pct`
  (NULL when the AP sends no BSS Load element) – a dated dataset change.
- `store.py`, `SCHEMA_VERSION = 7`, new table:

  ```sql
  CREATE TABLE IF NOT EXISTS ap_load_hourly (
    key           TEXT NOT NULL,           -- AP device key
    hour          INTEGER NOT NULL,        -- unix s of the UTC hour start
    channel       TEXT NOT NULL,           -- merged devices.adv_channel at the poll ('' = unknown)
    polls         INTEGER NOT NULL,
    clients_polls INTEGER NOT NULL DEFAULT 0, clients_max INTEGER, clients_sum INTEGER,
    qbss_polls    INTEGER NOT NULL DEFAULT 0, qbss_max INTEGER, qbss_sum INTEGER,
    util_max      REAL, util_sum REAL,
    first_ts      INTEGER NOT NULL, last_ts INTEGER NOT NULL,
    sent          INTEGER NOT NULL DEFAULT 0,  -- upload step (later): reset to 0 on every update
    PRIMARY KEY (key, hour, channel)) WITHOUT ROWID;
  ```

  Written in `_write_device()` in the same transaction as the observation and
  only when an observation row is written (`is_new_obs`, the same gating as
  the v5 per-poll columns, so backfilled and new hours mean the same). One
  `INSERT … ON CONFLICT DO UPDATE` per AP and poll (~70 per poll); max/sum
  with NULL handled explicitly (SQLite's scalar `max()` returns NULL if any
  argument is NULL).
- Migration v6 → v7: metadata only (the table comes from `_SCHEMA`); rollback
  = set `meta.schema_version` to 6 (the extra table is ignored by v6).
- `prune()`: `DELETE FROM ap_load_hourly WHERE hour < cutoff` with the same
  14-day retention; `counts()` includes it.
- Size on the Pi: ~1.9 k rows/day ≈ 0.2 MB/day, ~3 MB at 14 days.
- Tests (`collector/tests`): upsert accumulates within an hour; a channel
  change inside an hour gives two rows; no row without a new observation;
  BSS Load absent → no QBSS values; the QBSS flag in `test_shape.py`;
  v6 → v7 migration on a v6 file; prune.
- Docs: `collector/README.md` (schema), `wifi-sensor/CLAUDE.md` (the "Slim
  observations" paragraph gets a v7 note), and after the deploy a dated row in
  `docs/findings.md` §7 with the effective time read from the journal and
  `polls` (memory rule: dataset changes are dated).

### 9.2 Importer (`server/seed/import_snapshot.py`)

- Accept buffer versions 2–7.
- New table spec `ap_load_hourly` (optional: skipped when the snapshot has no
  such table): stage, `JOIN devices … type = 'ap'`, merge with the
  fuller-wins rule (4.2), `source = 'buffer'`.
- New stage `load_samples`: from any snapshot, the AP observation rows whose
  per-poll load columns are filled (`n_clients IS NOT NULL` – rows from
  buffers ≤ v5) into `stage_load_samples`, then `SELECT * FROM
  load_ap_load_samples(:sid, 'v5_obs')`. After the Pi's retention passes
  2026-10-17 no new snapshot carries such rows, and the stage is empty.
- Schema 9 (phase 2): one `sensor_uploads` row per import (span, buffer
  version, inserted rows, `buffer_bytes`); `export_snapshot.py` writes the
  source buffer's size and the export time into the snapshot copy's `meta`.
- Tests (`seed/tests`): the new specs generate the expected SQL; a v7 fixture
  buffer; the load stage selects only v5-era rows.

### 9.3 Backfill

| Window | Source | How |
|---|---|---|
| 2026-09-15 13:53 → 2026-10-01 07:37 | `backups/…20261003T103952Z.dump` (schema 6 `observations`, all columns) | `ops/backfill_ap_load_from_dump.sh <dump>`: `docker compose exec -T db pg_restore --data-only --table=observations -f - < dump` (`-f -` = stdout, required since PostgreSQL 12; the stream starts with `COPY public.observations (…) FROM stdin;`) → `ops/dump_load_samples.py --sensor-id 1` (stdlib; picks `ts, sensor_id, device_key, n_clients, qbss_stations, util_pct` by the header's column list, keeps rows with `n_clients`, wraps them in `CREATE TEMP TABLE stage_load_samples … COPY … load_ap_load_samples(1)`) → psql. No refresh: nothing reads the table before API v2, the next import refreshes. Nothing is restored into a table; ~3.4 M lines stream through, ~2.3 M staged in a temp table (~150 MB temp). |
| 2026-09-26 20:25 → 2026-10-03 11:00:02 | `buffer-snapshot-20261010.db` | re-import with the new importer: time series rows are deduplicated as always, the load stage fills the 10-01 07:37 → 10-03 11:00 window (and re-confirms the overlap) |
| check | `buffer-snapshot.db` (v5) | optional third run: it must update 0 rows (same data as the dump) |
| 2026-10-03 11:00:20 → v7 deploy | – | **no source** (permanent gap; the UI marks it "client counts were not recorded") |

Afterwards Claude runs `tests/sql/check_schema8.sql` (read-only): load hours
per day vs capturing hours from `sensor_hourly`, rows per source, a sample of
hours compared with aggregates computed read-only from the snapshot file.

---

## 10. Phases

Every phase ends deployable, with the old dashboard working until its pages
are replaced. Small commits, sensor and server separate. Commands for the
developer use the real paths (`~/wifi-audit-private` on the Pi, `~/wifi-audit`
on the server); `set -a; . ./.env; set +a` is assumed in `server/` where
`$POSTGRES_USER`/`$POSTGRES_DB` appear.

### Phase 1 – load history (urgent: each day without v7 is lost)

Commits: (1) Sensor: buffer v7 `ap_load_hourly` + QBSS flag, tests, docs.
(2) Server: schema 8 + migration + fixture tests. (3) Server: importer v7 +
load samples, tests. (4) Server: dump backfill scripts + `check_schema8.sql`.
(5) Docs: findings §7 row (after the deploy). Commit 1 can be deployed alone
first; the server part must follow within 14 days (Pi retention).

On the Pi (as soon as commit 1 is pushed):

```sh
ssh -t pi
cd ~/wifi-audit-private && git pull
cd wifi-sensor && sudo ./install_collector.sh
journalctl -u wifi-sensor-collector -n 30 --no-pager    # "migrating buffer schema v6 -> v7", polls ok
```

Then Claude reads the effective time (journal, `polls`) read-only, checks that
`ap_load_hourly` fills (read-only SQLite, or the developer runs the one-liner
Claude gives) and writes the findings §7 row.

On the server (commits 2–4):

```sh
cd ~/wifi-audit && git pull && cd server
mkdir -p backups/keep && mv backups/wifi_audit-20261003T103952Z.dump backups/keep/   # out of backup.sh's prune pattern
./backup.sh                                           # copy the new dump off the box
sh ops/migrate_schema8.sh 2>&1 | tee ~/migrate8-$(date -u +%Y%m%dT%H%MZ).log
sh ops/backfill_ap_load_from_dump.sh backups/keep/wifi_audit-20261003T103952Z.dump 2>&1 | tee ~/backfill8-dump.log
python3 seed/import_snapshot.py buffer-snapshot-20261010.db --sensor pi-fri \
  | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
  2>&1 | tee ~/import-20261010-load.log
# keep buffer-snapshot.db and buffer-snapshot-20261010.db until phase 2 is verified
```

No api or frontend rebuild. Later imports of v7 snapshots work as before
(export on the Pi, scp, import).

**Next import window: 2026-10-18 – 2026-10-23** (decided 2026-10-11). Not
before: until ~2026-10-17 11:00 the Pi buffer still holds rows with the
per-poll load columns (last one 2026-10-03 11:00:02), and every import
re-reads them as load samples (~10 min of no-op work on the e2-micro; the
importer speed-up was skipped). Not after: the Pi's 14-day retention starts
dropping the first rows after the adapter recovery (2026-10-10 21:53:47, in
no snapshot yet) on 2026-10-24 21:53. Done when `check_schema8.sql` shows load rows
for every capturing hour 09-15 → 10-03 11:00 and from the v7 deploy on.

### Phase 2 – schema 9, rollups, fixture tests

Step 1 (decided 2026-10-11, runs at schema 8 or 9): restore from the
2026-10-03 dump the observation rows the schema-7 migration skipped - devices
that were not APs on 2026-10-03 but are APs now (15 devices, 726 AP-hours
before 2026-09-26 20:25 with load rows but no RSSI rows) -
`ops/restore_observations_from_dump.sh`, one transaction (ON CONFLICT DO
NOTHING, `refresh_ap_baselines()`), then vacuum and `refresh_rollups()` from
the earliest restored ts; read-only check `tests/sql/check_restore_observations.sql`.

Commits: schema 9 tables/functions/views in `schema.sql`; `security_*()` +
test; classification + test; `refresh_rollups()` extension + extended test;
events + test; migration/rollback scripts + `check_schema9.sql`;
`ops/seed_sensor_history.sql`; `ops/seed_account.example.sql` (account name,
`sensors.location`/`hardware`, managed and friendly networks with allowed
BSSIDs/OUIs); old routers `acked` → `status`; detector sink `acked` →
`status` + `updated_at`; importer `sensor_uploads` + `export_snapshot` meta.

```sh
cd ~/wifi-audit && git pull && cd server
./backup.sh                                           # copy off the box
sh ops/migrate_schema9.sh 2>&1 | tee ~/migrate9-$(date -u +%Y%m%dT%H%MZ).log   # incl. full refresh + fixture tests
docker compose build api && docker compose up -d api  # old routers read status
cp ops/seed_account.example.sql ~/seed_account.sql && nano ~/seed_account.sql
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ~/seed_account.sql
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT refresh_events(1)'
```

Claude then runs `check_schema9.sql` and `tests/perf/measure.sh` (allowed) to
confirm the old endpoints did not slow down. Done when all fixture tests pass
in the log and the old dashboard works.

### Phase 3 – API v2 read endpoints

Commits per endpoint group (sensors/overview, events, networks + clients,
aps, spectrum, sensors detail, settings reads), each with its unit tests and
`tests/perf/ep_v2_*.sql`.

```sh
cd ~/wifi-audit && git pull && cd server
docker compose build api && docker compose up -d api
docker compose run --rm api python -m unittest discover -s tests
```

Claude: `measure.sh` with the v2 scripts, read-only GET smoke tests through
Caddy on the box, the privacy scan over real responses. Done when every
endpoint meets 7.4.

### Phase 4 – frontend, page by page

4a shell (sidebar, top bar, `useFilters`, data layer, chart primitives,
tokens) + Overview · 4b Networks · 4c Spectrum · 4d Access points + AP detail ·
4e Events · 4f Sensors. Each sub-phase is deployable on its own:

```sh
cd ~/wifi-audit && git pull && cd server
docker compose build frontend && docker compose up -d frontend   # node build: the box's memory peak, nothing else building
```

Claude checks each page against its board (layout, copy, interactions,
tooltips, keyboard, hatching) and against the API numbers. Write actions are
visible but locked (`Write locked`).

### Phase 5 – Settings, write token, write endpoints, cleanup

Commits: write token dependency + `/auth/check`; write endpoints + tests;
Settings pages (4 tabs + token box); Confirm/False positive, classification,
note and "Managed/Friendly"/"Add to allowed" actions; optional `api_rw` role;
then a cleanup commit removing the old endpoints and `legacy/` pages.

```sh
cd ~/wifi-audit && git pull && cd server
echo "WRITE_TOKEN=$(openssl rand -base64 32)" >> .env      # keep it in the password manager
docker compose build api && docker compose up -d api       # compose passes WRITE_TOKEN to the api
docker compose build frontend && docker compose up -d frontend
# optional least privilege:
docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < ops/create_api_role.sql
#   then DATABASE_URL in .env with the api_rw role, docker compose up -d api
```

Out of scope for now (designed in 11, not built): upload after every poll
with config delivery, ranges beyond 30 days, live updates, login.

---

## 11. Later phases (designed now, built later)

### 11.1 Upload after every poll + config delivery

`POST /api/ingest/{polls,observations,devices,…,ap_load_hourly}` with a
per-sensor token (`sensor_tokens(sensor_id, token_hash)`); idempotent on the
existing natural keys (`ON CONFLICT DO NOTHING`, `ap_load_hourly`
fuller-wins, `devices` merge as the importer does); backlog in batches from
the 14-day buffer using the `sent` flags (the associations TODO in `store.py`
must be settled first). The response carries the newest `sensor_config`
version; the collector applies it (hop list via the installer's logic, a
restart of Kismet – which is a dataset boundary and is recorded) and reports
`confirmed_at` in the next upload. A `sensor_uploads` row per batch; rollups
refresh on a timer (every 5 min, current hour incrementally), not per
request.

### 11.2 Ranges beyond 30 days

Presets `90d` (1-day buckets), `180d` (1-week), custom `from`/`to`; heatmap
timeline ~180 cells; calendar view for clients from 60 days. New derived
tables (refresh_rollups): `ap_presence_daily(sensor_id, device_key, day,
hours, n_obs, rssi_median_of_medians)` and `ap_load_daily(sensor_id,
device_key, day, …)` per local day of the account; a half-year query reads
~180 rows per series. Retention: raw observations for a limited window
(archive as dump/Parquet first), hourly ≥ 1 year, daily forever;
TimescaleDB only when ingest volume justifies it.

### 11.3 Live updates

Step 1: `refetchInterval` 30 s with `If-None-Match`, paused while hidden
(8.3). Step 2: the ingest transaction ends with `NOTIFY data_changed
'<sensor_id>'`; the api LISTENs on one connection and serves
`GET /api/v2/stream` (SSE); the client invalidates the keys of that sensor
scope; Caddy `flush_interval -1` on that route.

### 11.4 Login

`users`, `sessions`, membership per account; `deps.get_account()` reads the
session instead of returning account 1; the write token is retired.

---

## 12. Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | **Disk**: 4.4 GB free, `observations` grows ~30–35 MB/day plus dumps (7 kept) → full in roughly 3–4 months | not part of this plan's scope, but a retention/archive decision is needed before then; the plan adds < 1 MB/day |
| R2 | Losing the backfill sources (dump pruned, snapshots deleted) | Phase 1 moves the dump out of the prune pattern; keep both snapshots until `check_schema8.sql` passes; copy the dump off the box |
| R3 | Load history gap grows until v7 runs | deploy commit 1 first, before the server part |
| R4 | Collector v7 bug on the Pi | metadata-only migration, rollback by `meta.schema_version = 6`, tests on the Mac, one upsert per AP per poll (negligible CPU) |
| R5 | Pre-v7 QBSS presence is inferred (`> 0`) | `source = 'v5_obs'` on those rows; documented in findings and in the UI tooltip |
| R6 | Kismet quirks: OWE transition sets bit 17; OWE labelled `-SAE`; `_counter()` stores missing `crypt_bits` as 0 | bit-based mapping with rule 2, last-known bits, string fallback; fixture cases for each |
| R7 | Multi-sensor: one event row per sensor; the same AP under different SSIDs per sensor | invisible with one sensor; merge rule designed when a second sensor exists |
| R8 | "Online/offline" before ingest: every sensor looks offline between imports | Q7 |
| R9 | 30-day distinct counts get slower as data grows | index-only hour index; measured in phase 3; daily presence table (11.2) is the fallback |
| R10 | DST (2026-10-25 inside the 30-day range soon) | bucket edges in local time, unit tests for both transitions |
| R11 | Two whitelists: `ap_baselines.trusted` (evil_twin detector) vs `networks.allowed_*` | the detector keeps its own until it is moved to `networks` in a later step (not in these phases) |
| R12 | Event noise (BEACONRATE as high, NOCLIENTMFP per client) | episodes, override table, Q4 |
| R13 | Privacy regressions (client MACs in Kismet alerts) | never copied, fixture assertion, API response scan |
| R14 | Frontend build on the 1 GB box | no new dependencies; build one image at a time (README) |
| R15 | Old/new API coexistence | `/api/v2` prefix; old endpoints removed only after the last page switched |

---

## 13. Open questions

**Answered 2026-10-11** (D1–D10 approved; decisions recorded in `SPEC.md`):
all as recommended below, with these additions – Q1: `security_label()` shows
"WPA2/WPA3 transition", distinct from plain WPA2-PSK; Q4: BEACONRATE **and
DISCONCODEINVALID** → low; all historical events start `open` and the
developer labels them (input for RQ2), so status changes stay auditable and a
bulk Confirm / False-positive action with one note is part of phase 5;
Q11: account time zone Europe/Bratislava, `pi-fri` hardware "Raspberry Pi
3B+ · RTL8821CU (USB)", account name and location are filled in by the
developer in `seed_account.sql` (placeholders in the example), mobile vendors
Apple, Samsung, Google, OnePlus, Motorola (no Xiaomi/Huawei).

| # | Question | Recommendation |
|---|---|---|
| Q1 | WPA2/WPA3 transition mode (PSK + SAE, MFP optional; 122 APs): `psk_no_mfp` or `strong` (as the boards show)? | `psk_no_mfp` – a WPA2-only client connects with PSK and without MFP |
| Q2 | TKIP offered next to CCMP: `legacy` or only TKIP-*only*? | `legacy` (weakest offered; 2 APs) |
| Q3 | Channel changes as events? | no – AP history and Spectrum only (21,544 changes in 25 days) |
| Q4 | Kismet alerts: episodes per AP/header (1 h gap) with Kismet's severity, and all historical events start `open` (~150)? | yes; consider overriding BEACONRATE (64 × high) to low |
| Q5 | AP detail RSSI: hourly instead of 2-hour medians? | yes (exact from the rollup) |
| Q6 | "Seen by sensors (RSSI)" and friendly-network RSSI = median of the hourly medians in the period? | yes (raw medians would break the 48 h rule) |
| Q7 | Sensor status before ingest: `online/offline` by the age of the last poll (always offline between imports), or `data until <time>` with online/offline only once ingest exists? | `data until <time>` until ingest |
| Q8 | AP "Active": seen within 5 min of its sensor's last capturing poll? | yes |
| Q9 | Presence classes of "Other networks": permanent = ≥ 90 % of capturing hours; daytime only = ≥ 80 % of its active hours 07–20 local on ≥ 2 days; transient = mobile, shown with the mean lifetime; else intermittent? | as written |
| Q10 | Settings → Sensors before config delivery exists: store the desired configuration and show "not delivered – sensor upload not built yet", or hide the Send button? | store + explicit note |
| Q11 | Account name (`KIS FRI`?), account time zone `Europe/Bratislava`, `pi-fri` location/hardware text, default mobile-vendor list (Apple, Samsung, Xiaomi, Huawei, OnePlus, Google, Motorola?) | confirm or give values |
| Q12 | Schema numbering 8 (load) + 9 (model), and `/api/v2` during the transition | yes |
| Q13 | Spectrum utilization heatmap rows: follow the band toggle (the board mixes both bands)? | follow the toggle |
| Q14 | The rule event "unknown BSSID with managed SSID" written by `refresh_events()` from the classification (independent of the batch evil_twin detector)? | yes |
