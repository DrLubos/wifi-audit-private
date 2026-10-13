# Dashboard v2 – specification

Status: approved design, 2026-10-10; amended 2026-10-11 with the changes
D1–D10 of `PLAN.md` section 2 and the developer's answers to its open
questions (marked "(D*n*)" / "(Q*n*)" below). Source of truth for the new
frontend and the schema/API work behind it. The boards in `boards/` show every
page; this file states what the boards cannot: definitions, data sources,
behaviour and the order of work. Where a board and this file disagree, this
file wins (e.g. the security level of WPA2/WPA3 transition mode, D5).

## 1. How to read the boards

- `boards/*.dc.html` are design mock-ups, one file per screen. Each has static
  HTML (layout, copy, components) and a `class Component` script whose
  `renderVals()` generates the illustrative data. The script is the precise
  answer to "what does this card show and how is it aggregated" (buckets,
  tooltips, gaps, labels). They do not run outside the design tool (they need
  its `support.js`); read them as text.
- All numbers are illustrative. Copy (English), layout, colors, interactions
  and the metric definitions below are binding; exact pixel values are not.
- Board → route:

| Board | Route | Notes |
|---|---|---|
| Main | `/` | Overview |
| Events | `/events` | |
| Networks | `/networks` | |
| Aps | `/aps` | |
| ApDetail | `/aps/:bssid` | account-level AP, keyed by BSSID |
| Spectrum | `/spectrum` | |
| Sensors | `/sensors` | |
| Settings | `/settings/networks`, `/settings/classification`, `/settings/detectors` | one route per tab, each shows only its section |
| SettingsSensors | `/settings/sensors` | `?sensor=pi-fri` preselects a sensor (link from the Sensors page) |

## 2. Global rules

- **UI language English.** Dates `10 Oct 17:52`, 24-hour clock, weekday
  `Sat 10 Oct`, decimal point, minus sign `−` for dBm. Times and day
  boundaries in the account's time zone `accounts.tz` = Europe/Bratislava
  (Q11; whole-hour offsets only, so bucket edges fall on UTC hours).
- **Terms** (use exactly these): Managed network (operator's own), Friendly
  network (someone else's, known and legitimate), Unknown, Suspicious, Ignored;
  Fixed AP, Mobile AP (hotspot); Clients; Event; Write locked.
- **No bystander PII.** Client devices only ever appear as counts. No client
  MAC, probe SSID or per-client row anywhere in the UI or API.
- **Charts.** Categorical colors in fixed order `#2a78d6`, `#eb6834`,
  `#1baf7a`, `#eda100`. Security levels use the status scale (critical
  `#d03b3b` open, serious `#ec835a` legacy, warning `#fab219` WPA2-PSK without
  MFP, good `#0ca30c` strong, neutral `#C9CDC5` unknown). Sequential blue ramp
  for heatmaps: `#cde2fb #9ec5f4 #6da7ec #3987e5 #256abf #184f95 #0d366b`.
  2px gaps between stacked segments, no dual axes, legend for 2+ series.
- **Every chart has hover and keyboard focus.** Stacked columns: the whole
  column is the hit target and the tooltip lists every series with a short
  colored line key, value bold first, label after, plus a total; the hovered
  column gets a light wash. Thin segments are therefore always readable.
  Heatmaps: hovered cell outlined, its text in the hint line above the map.
  Line charts (RSSI): crosshair snapping to the nearest bucket, tooltip with
  every sensor's value. Values reachable without hover via labels/tables.
- **Hatched** fill = outside the period or no sensor collecting. Never draw a
  gap as zero.
- **Performance.** A page never reads raw observations for a range > 48 h
  (existing rule). Cards load in parallel with skeletons; slow cards do not
  block fast ones.
- **Medians over a period** (per-sensor RSSI chips, friendly-network RSSI)
  are the median of the hourly medians in the period (Q6); the AP detail RSSI
  chart draws hourly medians (D8, Q5).

## 3. Shell

- **Sidebar** (dark, 232 px, `position: sticky; top: 0; height: 100vh` from
  900 px width; above the content on narrow screens): brand + account name,
  navigation, then two blocks separated by hairlines:
  - *Sensors*: one line per sensor, status dot + text (`pi-fri · online`,
    `pi-lab · offline 3 h`).
  - *System*: `API ok · database ok` and `schema vN` from `GET /api/health`
    (replaces today's HealthBadge in the top bar; same 60 s re-check).
- **Top bar**: page filters on the left, on the right `Latest data: <time of
  the newest stored poll>` and the `Write locked` / `Write unlocked` badge.
- **Filters persist across pages.** Source of truth is the URL query:
  `?sensor=all|<name>&range=24h|7d|30d`. A `useFilters()` hook reads/writes it;
  every navigation link keeps the current query; the last choice is also saved
  in `localStorage` and used when a page opens without parameters. Pages ignore
  filters they do not use (Events uses only `sensor`; Settings none).
- **Periods.** `24h` = rolling 24 h ending now, bucketed by hour starting at
  the current hour − 23 (e.g. Fri 18:00 … Sat 17:52). `7d` = 28 × 6-hour
  buckets aligned to 00/06/12/18. `30d` = 30 calendar days, today partial.
  The API takes `range` (and later `from`/`to`, section 10) and returns the
  buckets; the frontend never invents bucket boundaries.

## 4. Definitions

Account-level AP = one physical AP = one BSSID in the account, seen by one or
more sensors (`ap_sensors` maps it to each sensor's `devices` row). Signal
strength always belongs to the pair AP + sensor.

| Term | Definition |
|---|---|
| Mobile AP | `random_bssid` (locally administered bit) **or** vendor (`devices.manuf`) in the account's mobile-vendor list (default Apple, Samsung, Google, OnePlus, Motorola – editable; Xiaomi and Huawei left out as common home/LTE router vendors, Q11) **or** lifetime `last_seen − first_seen` < 2 h once it has ended (not seen for 1 h). Fixed AP = not mobile. |
| New AP | account-level `first_seen` falls inside the bucket/period. |
| Active AP in a bucket | at least one observation from any selected sensor in the bucket. Distinct APs are counted per bucket at read time from the AP-hour rows; per-hour counts are not stored, because they cannot be combined into 6-hour or daily buckets (D2). |
| Active AP now | seen within 5 min of its sensor's last collecting poll (Q8). |
| Collecting poll | `ok AND ds_running AND devices_active > 0` – a successful poll whose capture delivered devices; `ok` alone counted the 4-day adapter loss as successful (D9). Limited = collecting with `ds_hop_ok = false`. |
| Security level | SQL function `security_level(crypt, crypt_bits, mfp_sup, mfp_req)`, "weakest offered" (D5): `open` (no encryption, same rule as `ap_config_changes` uses for Open) · `legacy` (WEP, WPA1, or TKIP offered – also next to CCMP, Q2) · `psk_no_mfp` (any PSK AKM where MFP is not required, **including WPA2/WPA3 transition mode**, Q1) · `strong` (any Enterprise/802.1X, SAE or OWE without a PSK AKM, or PSK with MFP required) · `unknown`. `crypt_bits` is Kismet's 64-bit `dot11_crypt_*` set (verified in Kismet 2025-09-R1, `packet_ieee80211.h`; mapping and sources in `PLAN.md` section 5); fixture test like `ap_config_changes_test.sql`. `security_label()` names the configuration for tables and must keep **"WPA2/WPA3 transition"** apart from plain "WPA2-PSK" (Q1). |
| Network | an SSID within the account. A hidden AP whose real name is known (`name_seen`) belongs to that SSID; otherwise each hidden BSSID counts as its own network. |
| Network security | the weakest level among its BSSIDs active in the bucket. |
| Configured network | its SSID is in `networks` (managed or friendly). Mobile network = all its BSSIDs are mobile. Other = the rest. |
| Compliance (managed network) | every BSSID's actual security meets the network policy (`expected_security`, `require_mfp`); shown as `Compliant` or `Not met n/m`. |
| Clients of an AP in an hour | AP-reported station count from the BSS Load element (`qbss_stations`) when the AP sends it (Kismet's `dot11e_qbss` flag from buffer v7; before v7 inferred from a value > 0), otherwise Kismet `num_associated_clients` - which is cumulative: every client seen associated since Kismet began tracking the AP (Kismet 2025-09-R1 never prunes `associated_client_map`; checked 2026-10-12), so the fallback overstates and the UI labels it. Hourly value = max over the hour's polls. When several sensors see the AP, take the max across sensors. |
| Clients on managed APs | sum over managed APs per hour. Tile: peak hourly sum in the period, plus `hourly average` (24h) / `average 6-hour peak` (7d) / `average daily peak` (30d). Not unique people – the UI says so. |
| Channel utilization | AP-reported BSS Load channel utilization; per channel and hour = average over the APs on that channel that report it. |
| Open events | events with status `open`. |
| Presence (other networks) | permanent = seen in ≥ 90 % of the collecting hours of the period; daytime only = ≥ 80 % of its active hours between 07:00 and 20:00 local, on ≥ 2 days; transient = mobile, shown with the mean lifetime; otherwise intermittent (Q9). |

## 5. Pages

Each card states whether it follows the period. "Now" cards ignore it.

**Overview `/`** (sensor + period filters)
- *Current status* (now): sensor cards, Open events (count by severity), Managed
  networks compliant `m / n` with the first problem, Needs attention (open
  high/medium events + compliance findings).
- *Selected period*: header repeats the exact window. Tiles with sparklines:
  New APs (+ share of mobile), Open networks nearby (+ share of APs seen),
  Clients on managed APs (peak, average, time of peak). APs over time: stacked
  fixed/mobile per bucket, event dots, hatched gaps, tooltip with both series,
  total, sensor coverage notes and events in the bucket. Security of nearby
  APs: 100 % bar + legend with counts; hovering a segment highlights its row.
- Recent events (now): the 5 newest, link to Events.

**Events `/events`** (sensor filter only)
- Unified, newest-first feed: detector results, Kismet alerts, security-
  relevant configuration changes, sensor offline/online. Columns: time,
  severity, type, event, AP/network, BSSID (full), sensors, status, actions.
  - Kismet alerts are **episodes**: alerts of one header on one AP with gaps
    ≤ 1 h form one event (Q4). SOURCEERROR/SOURCEOPEN are sensor health, not
    events (they feed the capture-crash count and the outage periods).
    Severity is Kismet's own (`info 0, low 5, medium 10, high 15, critical
    20`, `alertracker.h`) with overrides: BEACONRATE and DISCONCODEINVALID →
    low (D10, Q4).
  - **Channel changes are not events** (dynamic channel selection: 21,544 in
    25 days); they stay in the AP's configuration history and in Spectrum
    (Q3). Configuration events are security changes (crypt, crypt bits, MFP)
    graded by the level before and after.
  - The rule event "unknown BSSID broadcasting a managed SSID" comes from the
    classification (Settings → Classification), independent of the batch
    evil_twin detector (Q14).
  - All events, historical ones included, start `open`. Status changes are
    the operator's labels (input for the false-positive analysis, RQ2): they
    keep `status_at` and `status_note` and are never overwritten by a refresh
    or a detector re-run.
- Filters: status (open / confirmed / false positive; Open by default - the
  work queue -, `status=all` for every status), type, "Medium and above",
  managed networks only, BSSID/SSID search. Keyset pagination on the rows'
  `(ts, id)`, 50 rows per page, "Newer"/"Older"; never OFFSET.
- Grouped rows (display only, decided 2026-10-12): repeated info / low events
  of one AP, type and status on one local day are one row with their number,
  expandable to the events; a bulk label can take the whole row.
- Expanded row: "Why it fired" (evidence) and "Who saw it" (per-sensor RSSI,
  first/last seen, client count). Actions Confirm / False positive need the
  write token and a fixed reason (decided 2026-10-12, schema 11), plus an
  optional note, so RQ2 is counted by reason: Confirm - security-relevant,
  expected / benign (true, but not a security issue), staged test (the
  developer's own attack runs); False positive - rule defect or
  misattribution, sensor or environment artefact.
- Export for RQ2 (behind the write token): every event with its outcome and reason
  (open ones too) and every change of a label, as CSV - "Labels" and "Label history"
  on the Events page; the detail of an event lists its label history.
- Bulk action (phase 5): select several rows → Confirm / False positive with
  one note for all of them; each row gets its own status change in the audit
  log.

**Networks `/networks`** (sensor + period)
- Rogue banner when an unknown BSSID broadcasts a managed SSID.
- Networks over time (configured / mobile / other) and Network security over
  time (4 levels); both share the x-axis and highlight the same bucket on
  hover. Security changes in the period (downgrade/upgrade list from
  `ap_config_changes`).
- Managed networks table (policy vs actual, compliance, sensors per network,
  clients now; expandable BSSID rows). Friendly networks. Other networks with
  "Managed"/"Friendly" quick actions (write token).
- Clients on managed networks heatmap: views *Day by day* (rows = calendar days
  of the period, 24 hourly cells) and *Average week* (rows Mon–Sun, each cell
  the mean over the period's matching weekday-hours; disabled for 24h).

**Access points `/aps`** (sensor + period): search, group by (none / SSID /
sensor), classification and band/security chips with counts, table with
"Seen by sensors (RSSI)" chips ordered strongest first, pagination.

**AP detail `/aps/:bssid`**: identity, security (+ policy), current load,
classification control (write token), RSSI per sensor over 7 days with the
baseline band and crosshair tooltip, per-sensor statistics table (median,
robust σ, floor share, observations), clients heatmap (average week),
utilization of its channel per 6-hour window with tooltip (average, hourly
peak), configuration history, events of this AP, note (write token).

**Spectrum `/spectrum`** (sensor + period): band toggle (it also selects the
heatmap rows: channels of that band with at least one reporting AP, Q13);
APs per channel – each AP once, on its primary channel (most polls in the
period) – with tooltip (count, average utilization, overlap/DFS note); channel
plan issues
(overlapping 2.4 GHz channels, 40 MHz in 2.4 GHz, busiest channel, channel-
change frequency, 1/5/9/13 vs 1/6/11 counts); utilization heatmap with views
*Average day* (24 cells) and *Timeline* (24h: 24 × 1 h, 7d: 168 × 1 h, 30d:
180 × 4-hour blocks whose tooltip gives average and peak).

**Sensors `/sensors`**: until ingest exists the status reads `data until
<time of the last imported poll>` instead of online/offline, which would show
every sensor offline between imports (Q7). One card per sensor (status,
hardware, last poll, last upload, coverage, capture crashes, buffer, APs seen and seen only by this
sensor, current capture configuration, pending change) with a "Sensor
settings" link to `/settings/sensors?sensor=<name>`. Coverage per day for 14
days (sensor toggle; successful / limited channels / outage / not installed)
with tooltip. Outages and limited periods table.

**Settings** (each tab its own route; write-token box on every tab):
- Networks: managed/friendly networks with expected security, MFP required,
  allowed BSSIDs/OUIs; add/remove.
- Classification: automatic rules (ignore randomized hotspots, managed SSID
  from a non-allowed BSSID → suspicious, allowed vendor + managed SSID →
  managed, ignore APs weaker than X dBm). Manual classification always wins.
- Detectors: deauth bursts/window, evil twin (b) k and min dB, guard period
  after channel change, sensor-offline alert minutes.
- Sensors: table with per-row and "All" selection; one form applies to every
  selected sensor (channel plan: 89 / 20 MHz 37 / locked channel / custom
  list with the coprime-stride check; dwell; poll interval - the "without
  empty DFS 59" plan is not offered: every DFS channel 52-136 carried APs,
  PLAN 5a). Sensors are added (empty), edited and renamed in modals, and deleted only
  when they never collected anything, with the name typed (PLAN 5b follow-up). Capture
  hardware (decided 2026-10-13): one read-only adapter line per sensor (identity VID:PID +
  MAC; seeded now, reported by the sensor in the live-upload phase) and "Add hardware
  change" - a dated entry of the configuration history (when, an optional note such as
  "swapped antenna") that is a data boundary like a channel-plan change; RSSI baselines
  never span it (evil twin (b) would fire after a swap). There is no antenna record: it
  is passive, undetectable by the sensor, and its gain feeds no computation. Mixed current plans → warning. Upload is **not** a setting
  (always after every poll, section 10). Configuration history with "data
  boundary" marking – a channel-plan change splits measurements for analysis.
  Until configuration delivery exists (section 10) "Send" stores the desired
  configuration and the page says plainly that it is not delivered yet (Q10).

## 6. Data model (schemas 8 and 9 – details in `PLAN.md` section 4)

Two schema versions (D1): **8** = the AP load history only (`ap_load_hourly`,
phase 1, additive), **9** = everything else below (phase 2).


- `accounts(id, name)`; `sensors.account_id`.
- `aps(id, account_id, bssid UNIQUE per account, classification, note,
  first_seen, last_seen)` and `ap_sensors(ap_id, sensor_id, device_key)`;
  keep per-sensor `devices`, observations and rollups as they are.
  `aps.classification` holds only the **manual** classification (NULL = the
  rules decide); the effective classification and `classified_by
  manual|rule` are computed at read time from the current settings. No
  `network_id`: an AP's network is its SSID, or `name_seen` for a hidden AP
  (D3).
- `networks(id, account_id, ssid, kind managed|friendly, expected_security,
  require_mfp, allowed_bssids macaddr[], allowed_ouis macaddr[] (truncated
  MACs), note)` (D3).
- `account_settings(account_id, …)` as typed columns: classification rules,
  detector parameters, `mobile_vendors text[]`.
- Events are **rows in `detections`** (D6), with `kind`
  (`detection|kismet|config|sensor`), `status`
  (`open|confirmed|false_positive`, migrated from `acked`), `status_at`,
  `status_note`, `ts_end`, `updated_at`. Detector rows are written by the
  detectors as today; Kismet episodes, security configuration changes and
  sensor outages are derived by `refresh_events()` inside the refresh, which
  never touches `status`, `status_at` or `status_note`.
- **AP load history (schema 8, needed by every clients/utilization chart):**
  `ap_load_hourly(sensor_id, device_key, hour, channel, polls, clients_polls,
  clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum, util_max,
  util_sum, source buffer|v5_obs)` – sums and counts instead of means so
  partial hours merge exactly ("the row with more polls wins"), the AP's
  advertised channel in the key because campus APs change channel many times
  a day (D4). Source data like `observations`, not written by the refresh.
  See section 9 for where the data comes from.
- Period cards count distinct APs / networks per bucket from the AP-hour
  rows (`ap_rssi_hourly`, `ap_load_hourly`) at read time (D2); a daily
  presence level for ranges beyond 30 days is designed (section 10,
  `PLAN.md` 11.2), not built.
- Sensor configuration: `sensor_config(sensor_id, version, channel_plan,
  channels, dwell_s, poll_s, created_at, confirmed_at NULL)`; newest row =
  desired, `confirmed_at` set when the sensor reports it applied it.
- Everything derived is still written only by `refresh_rollups()` (or its
  successor) and covered by the scratch-schema fixture tests.

## 7. API (proposed)

Read endpoints are public until login; every write needs the write token
(section 8). All period endpoints take `sensor` and `range` and return buckets
with explicit `start`, `end`, coverage per sensor and `null` for no data.
The new endpoints live under **`/api/v2/`** while the current ones stay for
the old pages; the old ones are removed after the frontend switch (D7). The
paths below are relative to that prefix; full list with parameters and
response shapes in `PLAN.md` section 7.

- `GET /api/health` (exists) · `GET /api/sensors` (status for sidebar/cards)
- `GET /api/overview?sensor&range` (current-status block + period tiles + AP
  time series + security distribution) – may be split per card
- `GET /api/events?sensor&status&type&managed&q&before=<ts,id>` ·
  `GET /api/events/{id}` · `POST /api/events/{id}/status`
- `GET /api/networks?sensor&range` · `GET /api/networks/timeseries?…` ·
  `GET /api/clients/heatmap?sensor&range&view=days|week`
- `GET /api/aps?…` (paged) · `GET /api/aps/{bssid}` · `GET /api/aps/{bssid}/rssi`
  · `GET /api/aps/{bssid}/load` · `PUT /api/aps/{bssid}/classification` ·
  `PUT /api/aps/{bssid}/note`
- `GET /api/spectrum?sensor&range&band` · `GET /api/spectrum/utilization?…&view`
- `GET /api/sensors/{name}/coverage?days=14` · `GET /api/sensors/{name}/outages`
- `GET|PUT /api/settings/networks|classification|detectors` ·
  `GET /api/settings/sensors` · `POST /api/settings/sensors` (body: sensor
  names + config)
- Keep ETag/304 on everything derived from rollups (today's
  `sensor_summary.refreshed_at` pattern). The tag of a period endpoint also
  carries the current bucket (hour / 6-hour window / day), the account's
  write version and the newest event change.

## 8. Write token (until login)

`WRITE_TOKEN` in the gitignored `.env`. Mutating requests send
`Authorization: Bearer <token>`; constant-time comparison; 401 without it.
The UI's "Unlock" stores the token in `sessionStorage` only and flips the
badge to `Write unlocked`. Login (accounts, users, sessions) is a later phase;
build reads so that the account comes from one place (today: the single
account) and login can replace it.

## 9. Known data gap: client and utilization history

From collector buffer v6 (2026-10-03 11:00:20 UTC) to buffer v7 neither the
Pi nor the server keeps per-poll client counts or utilization – only the
latest value (`devices.cur_*`). That window has no load history and the
charts mark it "client counts were not recorded"; buffer v7 records
`ap_load_hourly` again (phase 1).

- The collector needs to record AP load again, compactly: e.g. an hourly
  aggregate per AP in the buffer (`ap_load_hourly`, a few thousand rows a day)
  that the importer (and later the ingest endpoint) loads. That is a
  `wifi-sensor/` change: follow `wifi-sensor/CLAUDE.md`, bump the buffer
  version, and the developer deploys it on the Pi. Do it early so history
  accumulates while the frontend is built.
- History before 2026-10-03 11:00 UTC still exists in the old per-poll columns
  (`n_clients`, `qbss_stations`, `util_pct`). `observations_v5` has been
  dropped (checked 2026-10-11); the data is in files on the server box, which
  must be kept until the backfill is verified:
  - `backups/wifi_audit-20261003T103952Z.dump` (pre-migration dump,
    2026-09-15 13:53 → 2026-10-01 07:37 UTC) – backfilled with
    `ops/backfill_ap_load_from_dump.sh`;
  - `buffer-snapshot-20261010.db` (load values up to 2026-10-03 11:00:02)
    and `buffer-snapshot.db` – re-imported with the v7 importer, which turns
    the per-poll columns into hourly rows (`source = 'v5_obs'`).

## 10. Later phases (design for them now, build them later)

**Upload after every poll (ingest).** The collector POSTs each poll's slim
records to `/api/ingest/*` with a per-sensor token; idempotent inserts on the
existing keys (`ON CONFLICT DO NOTHING`); after an outage it sends the backlog
from its 14-day buffer in batches; the response carries the desired
`sensor_config` version so a sensor applies a new configuration within one
poll; rollups refresh on a timer (e.g. every 5 min, current hour
incrementally), not per request.

**Longer ranges (60 days, a quarter, half a year, custom from–to).** Add
presets `90d`, `180d` and a custom range. Keep the number of marks constant
by growing the bucket: bars ~30–90 per chart (90d: 1 day, 180d: 1 week);
heatmap timeline ~180 cells (90d: 12 h, 180d: 1 day); *Average day* and
*Average week* work for any length; for 60+ days the clients heatmap offers a
calendar view (rows = weeks, columns = weekdays, cell = daily peak) instead of
day-by-day. Data comes from daily rollups, so a half-year query reads ~180
rows per series. Retention: raw 30-s observations for a limited window, hourly
rollups at least a year, daily forever; archive raw data (dump/Parquet) before
it ages out, the thesis evaluation needs it. TimescaleDB continuous
aggregates/retention are an option once ingest volume justifies it (the
schema is already hypertable-ready); not now.

**Live updates without refresh.** Step 1: refetch the visible page every 30 s
with `If-None-Match` (cheap 304 when nothing changed), paused while the tab is
hidden. Step 2: Server-Sent Events – the ingest transaction ends with
`NOTIFY data_changed '<sensor_id>'`, the API LISTENs and pushes an event on
`GET /api/stream`, the client re-fetches only the affected cards; Caddy needs
`flush_interval -1` on that route. WebSockets are not needed (one-way).
Structure the frontend data layer (one fetch/cache hook, query keys per card)
so step 2 only adds an invalidation trigger.

## 11. Constraints

- e2-micro (1 GB RAM, shared core): no extra services, no heavy chart
  library. uPlot stays for the RSSI line chart; bars and heatmaps are small
  SVG/HTML components.
- Server operating rules in `server/CLAUDE.md` apply: Claude Code reads the
  server (psql as `claude_ro`) but never writes the database, never runs
  `docker compose up/down/build/rm`, never edits server files; the developer
  runs migrations, imports, backups and deploys.
- English in code, comments, commits and docs.
