# Dashboard v2 – specification

Status: approved design, 2026-10-10. Source of truth for the new frontend and
the schema/API work behind it. The boards in `boards/` show every page; this
file states what the boards cannot: definitions, data sources, behaviour and
the order of work.

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
  boundaries in Europe/Bratislava (`sensors.tz` defaults to UTC – check the
  rows and set it, or add an account time zone).
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
| Mobile AP | `random_bssid` (locally administered bit) **or** vendor in the account's mobile-vendor list **or** lifetime `last_seen − first_seen` < 2 h (once it has ended). Fixed AP = not mobile. |
| New AP | account-level `first_seen` falls inside the bucket/period. |
| Active AP in a bucket | at least one observation from any selected sensor in the bucket. |
| Security level | SQL function `security_level(crypt, crypt_bits, mfp_sup, mfp_req)` → `open` (no encryption, same rule as `ap_config_changes` uses for Open) · `legacy` (WEP, WPA1, TKIP-only) · `psk_no_mfp` (WPA2-PSK where MFP is not required) · `strong` (any Enterprise/802.1X, SAE/WPA3, or PSK with MFP required) · `unknown`. Derive from Kismet's `crypt_bits` definitions – verify them in Kismet's source, do not guess – with a fixture test like `ap_config_changes_test.sql`. |
| Network | an SSID within the account. A hidden AP whose real name is known (`name_seen`) belongs to that SSID; otherwise each hidden BSSID counts as its own network. |
| Network security | the weakest level among its BSSIDs active in the bucket. |
| Configured network | its SSID is in `networks` (managed or friendly). Mobile network = all its BSSIDs are mobile. Other = the rest. |
| Compliance (managed network) | every BSSID's actual security meets the network policy (`expected_security`, `require_mfp`); shown as `Compliant` or `Not met n/m`. |
| Clients of an AP in an hour | AP-reported station count from the BSS Load element (`qbss_stations`) when the AP sends it, otherwise Kismet `num_associated_clients`. Hourly value = max over the hour's polls. When several sensors see the AP, take the max across sensors. |
| Clients on managed APs | sum over managed APs per hour. Tile: peak hourly sum in the period, plus `hourly average` (24h) / `average 6-hour peak` (7d) / `average daily peak` (30d). Not unique people – the UI says so. |
| Channel utilization | AP-reported BSS Load channel utilization; per channel and hour = average over the APs on that channel that report it. |
| Open events | events with status `open`. |

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
- Filters: status (open / confirmed / false positive), type, managed networks
  only, BSSID/SSID search. Keyset pagination on `(ts, id)`, 50 per page,
  "Newer"/"Older"; never OFFSET.
- Expanded row: "Why it fired" (evidence) and "Who saw it" (per-sensor RSSI,
  first/last seen, client count). Actions Confirm / False positive need the
  write token.

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

**Spectrum `/spectrum`** (sensor + period): band toggle; APs per channel with
tooltip (count, average utilization, overlap/DFS note); channel plan issues
(overlapping 2.4 GHz channels, 40 MHz in 2.4 GHz, busiest channel, channel-
change frequency, 1/5/9/13 vs 1/6/11 counts); utilization heatmap with views
*Average day* (24 cells) and *Timeline* (24h: 24 × 1 h, 7d: 168 × 1 h, 30d:
180 × 4-hour blocks whose tooltip gives average and peak).

**Sensors `/sensors`**: one card per sensor (status, hardware, last poll, last
upload, coverage, capture crashes, buffer, APs seen and seen only by this
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
  selected sensor (channel plan: 89 / 20 MHz 37 / without empty DFS 59 /
  locked channel / custom list with the coprime-stride check; dwell; poll
  interval). Mixed current plans → warning. Upload is **not** a setting
  (always after every poll, section 10). Configuration history with "data
  boundary" marking – a channel-plan change splits measurements for analysis.

## 6. Data model (schema 8, recommended – propose changes in the plan)

- `accounts(id, name)`; `sensors.account_id`.
- `aps(id, account_id, bssid UNIQUE per account, classification, classified_by
  manual|rule, network_id NULL, note, first_seen, last_seen)` and
  `ap_sensors(ap_id, sensor_id, device_key)`; keep per-sensor `devices`,
  observations and rollups as they are.
- `networks(id, account_id, ssid, kind managed|friendly, expected_security,
  require_mfp, allowed_bssids macaddr[], allowed_ouis text[])`.
- `account_settings(account_id, classification rules, detector parameters,
  mobile_vendors)` as typed columns or one validated jsonb.
- Events: give `detections` a `status` (`open|confirmed|false_positive`,
  migrated from `acked`) and decide in the plan whether Kismet alerts,
  configuration changes and sensor outages become rows there or a UNION view.
- **AP load history (new, needed by every clients/utilization chart):**
  `ap_load_hourly(sensor_id, device_key, hour, polls, clients_max,
  clients_avg, qbss_max, util_avg, util_max)`. See section 9 for where the
  data comes from.
- Rollups for the period cards (bucketable from hourly rows):
  per account/sensor and hour – active fixed/mobile APs, new APs, open APs,
  networks by category and by security level, clients on managed APs. Plus a
  daily level so ranges beyond 30 days stay cheap (section 10).
- Sensor configuration: `sensor_config(sensor_id, version, channel_plan,
  channels, dwell_s, poll_s, created_at, confirmed_at NULL)`; newest row =
  desired, `confirmed_at` set when the sensor reports it applied it.
- Everything derived is still written only by `refresh_rollups()` (or its
  successor) and covered by the scratch-schema fixture tests.

## 7. API (proposed)

Read endpoints are public until login; every write needs the write token
(section 8). All period endpoints take `sensor` and `range` and return buckets
with explicit `start`, `end`, coverage per sensor and `null` for no data.

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
  `sensor_summary.refreshed_at` pattern).

## 8. Write token (until login)

`WRITE_TOKEN` in the gitignored `.env`. Mutating requests send
`Authorization: Bearer <token>`; constant-time comparison; 401 without it.
The UI's "Unlock" stores the token in `sessionStorage` only and flips the
badge to `Write unlocked`. Login (accounts, users, sessions) is a later phase;
build reads so that the account comes from one place (today: the single
account) and login can replace it.

## 9. Known data gap: client and utilization history

Since collector buffer v6 (2026-10-03 11:00:20 UTC) neither the Pi nor the
server keeps per-poll client counts or utilization – only the latest value
(`devices.cur_*`). Until that changes, the clients heatmap, the clients tile,
the Spectrum utilization heatmap and the AP utilization chart have no data.

- The collector needs to record AP load again, compactly: e.g. an hourly
  aggregate per AP in the buffer (`ap_load_hourly`, a few thousand rows a day)
  that the importer (and later the ingest endpoint) loads. That is a
  `wifi-sensor/` change: follow `wifi-sensor/CLAUDE.md`, bump the buffer
  version, and the developer deploys it on the Pi. Do it early so history
  accumulates while the frontend is built.
- History before 2026-10-03 11:00 UTC still exists in the old per-poll columns
  (`n_clients`, `qbss_stations`, `util_pct`):
  - on the server in `observations_v5` (data up to the last import,
    2026-10-01 07:37 UTC) and in the pre-migration dump – backfill
    `ap_load_hourly` from it before `observations_v5` is dropped;
  - for 2026-10-01 07:37 → 2026-10-03 11:00 only in the Pi buffer, which keeps
    14 days. The next snapshot export carries those rows; keep that snapshot
    file and let the backfill read it (the current importer ignores these
    columns).

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
