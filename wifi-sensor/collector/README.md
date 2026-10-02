# wifi-sensor collector

Thin, passive data collector for the Wi-Fi monitoring sensor (Milestone 2,
part 1). It polls the local Kismet REST API, reduces every active device to a
compact record and appends it to a SQLite buffer. It contains no detection and
no upload; the upload step (store-and-forward over HTTPS) is added later and
reads this buffer.

```
Kismet (REST, localhost:2501)
   |  every POLL_INTERVAL s: POST /devices/views/all/last-time/{since}/devices.json
   |                         GET  /alerts/last-time/{since}/alerts.json
   |                         GET  /system/status.json, /datasource/all_sources.json
   v
collector.py  ->  shape.py (Kismet JSON -> compact record)  ->  store.py (SQLite, WAL)
```

## Files

| File | Purpose |
|---|---|
| `collector.py` | Main loop, configuration, logging (stdout -> journald) |
| `kismet_client.py` | Read-only REST client (`requests`, Basic auth) |
| `shape.py` | Field list requested from Kismet and the raw -> record mapping |
| `store.py` | Schema and writes of the SQLite buffer |
| `collector.conf.example` | Runtime configuration template |
| `tests/` | `python3 -m unittest discover -s collector/tests` (stdlib only) |
| `../systemd/wifi-sensor-collector.service` | systemd unit template |
| `../install_collector.sh` | Installer, run on the Pi with `sudo` |
| `../analysis/` | One-off read-only analysis scripts for the buffer (not part of the service, see its README) |

## Installation on the Pi

```
cd wifi-sensor            # the sensor folder of the monorepo clone
sudo ./install_collector.sh
```

Copies the code to `/opt/wifi-sensor/collector` (and the analysis scripts to
`/opt/wifi-sensor/analysis`), creates `/etc/wifi-sensor/collector.conf` from the
example (kept on re-runs), creates `/var/lib/wifi-sensor` for the buffer, and
enables `wifi-sensor-collector.service` running as the sensor user (group
`kismet`), started after `kismet.service`. Credentials are read from Kismet's own
`~/.kismet/kismet_httpd.conf`; nothing is stored twice. Re-run the installer
after every code change.

```
journalctl -u wifi-sensor-collector -f
INFO poll ok: total=237 active=94 skipped=0 new_obs=61 alerts=0/0 ds=up 210ms
```

`skipped` counts devices that `shape.py` could not turn into a record (each one
is also logged with its reason); it should stay at 0, a steady non-zero value
points to a shaping regression.

## What one poll does

1. `since` = Kismet timestamp of the previous successful poll (persisted in
   `meta.last_kismet_ts`; a fresh buffer starts at 0 and takes a full snapshot).
2. Ask Kismet for devices active after `since - POLL_OVERLAP` with the fixed
   field list `shape.DEVICE_FIELDS` (about 1.2 KB per device instead of ~11 KB).
3. Shape every device into a record; a device whose Kismet `last_time` did not
   advance since its last stored observation is skipped, so the overlap window
   never duplicates rows.
4. Write everything in one transaction: `polls`, `devices` (+ config history),
   `observations`, `device_freq_hist`, `associations`, `probes`, `alerts`.
5. Once per hour, delete `observations` and `polls` older than `RETENTION_DAYS`.

A failed poll (Kismet restarting, datasource error) is recorded in `polls`
with `ok = 0` and the loop continues; the service is never taken down by it.

## Buffer schema (SQLite, `DB_PATH`)

| Table | One row per | Contents |
|---|---|---|
| `polls` | poll | collector/Kismet timestamps, device counts, datasource state, hop-list coverage (`ds_hop_n`, `ds_hop_visited`, `ds_hop_ok`), duration, error |
| `devices` | device | key, MAC, type, manufacturer, first/last seen; for APs the advertised configuration: SSID, cloaked, `crypt` (Kismet crypt string), `crypt_bits`, MFP supported/required, advertised channel, HT mode, beacon rate, country; and (v6) the latest AP load: `cur_n_clients`, `cur_qbss_stations`, `cur_util_pct`, `cur_at` |
| `device_config_history` | AP configuration change | the configuration that was replaced, with the poll time of the change; only when a known value changes to a different known value (field-wise merge, see below) |
| `observations` | active device per poll | written from v6: Kismet `last_time`, RSSI (`rssi`, adapter floor values stored as NULL with `rssi_floor = 1`, v5), cumulative `pk_total` and `pk_data`; AP only: `disconnects` (size of the current deauth/disassoc burst, not a counter), `disconnects_last` (unix second of the last deauth/disassoc frame, NULL until one is seen), `bss_timestamp` (uptime). The other columns (`freq_khz`, `channel`, `rssi_min`/`rssi_max`, `pk_tx`, `pk_rx`, `bytes`, `n_clients`, `qbss_stations`, `util_pct`, `ie_checksum`, `beacon_fp`, `bssid`) are NULL from v6 and hold values only in rows written before it |
| `device_freq_hist` | device × frequency | cumulative packet count per frequency (Kismet `freq_khz_map`) |
| `client_bssids` | client × BSSID | (v6) first/last poll at which the client's Kismet `last_bssid` was this BSSID; replaces `observations.bssid` |
| `associations` | AP × client MAC | first/last poll at which the client MAC was listed in the AP's associated-client map (Kismet gives no per-client times and never drops entries, so `last_seen` is "still listed", not "last frame") |
| `probes` | client × SSID | first/last time the SSID was probed for (`""` = wildcard) |
| `alerts` | Kismet alert | header, class, severity, MACs, channel, text, full JSON; deduplicated by Kismet's hash |
| `meta` | key | schema version, resume point |

Cumulative counters are stored as Kismet reports them; deltas are derived when
the data is analysed, which keeps the collector free of state and robust to
missed polls. `sent` columns exist for the later upload step.

Schema v6 slims the observation row (`docs/findings.md` section 7 for the
effective time). It keeps what the server's detectors and dashboard read -
`last_time`, `rssi`/`rssi_floor`, `pk_total`, `pk_data`, `disconnects`,
`disconnects_last`, `bss_timestamp` - and writes NULL into the other columns
instead of dropping them: no rewrite of the buffer's largest table on the Pi,
the 14-day retention ages the old values out, and the v5 code can still run on
the file. `last_time` stays because the collector needs Kismet's last_time
anyway (devices, the new-packets check) and the column is `NOT NULL`. The AP
load moves to `devices` as the latest value (`cur_n_clients`,
`cur_qbss_stations`, `cur_util_pct`, `cur_at` = the poll ts), written by the
devices upsert of every poll the AP is seen and never through `merge_config`
or `device_config_history`: it is not configuration. A client's BSSID goes to
`client_bssids`, deduplicated like `associations`. The fields no longer stored
are not requested from Kismet either (`shape.DEVICE_FIELDS`: no device
channel/frequency, min/max signal, tx/rx packets, data size, IE checksum,
beacon fingerprint). Migration: `ALTER TABLE devices ADD COLUMN` and the new
table, metadata only; to roll the code back set `meta.schema_version` to `5`.
Analysis scripts take an AP's band from `devices.adv_channel` (fallback: the
busiest frequency of `device_freq_hist`).

Schema v5 added `observations.rssi_floor`. The RTL8821CU (`rtw88_8821cu`)
reports two clamp values: **-106 dBm** for CCK frames (every 2.4 GHz beacon;
`rtw8821c.c` computes `lna_gain_table[lna] - 2 * vga`, and -106 is the smallest
value that formula can produce) and **-120 dBm** for OFDM frames
(`max(PWDB - 110, -120)`). A floor means "at or below the floor", not a signal
level: 3.6 % of all readings were exactly -106 (`docs/findings.md` sections 10
and 11). From v5 `rssi`, `rssi_min` and `rssi_max` are NULL for a floor value
and `rssi_floor = 1` records that the last reading was one, so the per-AP floor
share (an eligibility criterion for RSSI baselines) stays countable. The one
definition is `dataset_rules.py`, used by `shape.py` and the analysis scripts;
rows written before v5 keep the raw -106/-120 in `rssi`, so every reader filters
them (`dataset_rules.rssi_value()` / `sql_rssi()`). A different capture adapter
needs its floors re-derived. Migration as for v3 (`ALTER TABLE ADD COLUMN`); to
roll the code back set `meta.schema_version` to `4`.

AP configuration (advertised channel, HT mode, beacon rate, country, ...) is
merged field by field (`store.merge_config`): a NULL from Kismet means "not in
this record, keep the stored value"; only a known value replaced by a
different known value writes a `device_config_history` row and bumps
`config_changed_at`, and unknown -> known fills the field silently. Before this
(until the v5 collector deployment) Kismet's alternating empty/real
`last_beaconed_ssid_record` (channel 0, empty HT mode, beacon rate 0 -> NULL)
wrote a history row on almost every poll for some APs and blanked their
`devices` configuration in between; 42 % of the server's history rows are this
artefact (`docs/findings.md` section 11). Consequence of the merge: a field
that genuinely disappears (e.g. a dropped country IE) is no longer recorded.

Schema v4 added `polls.ds_hop_n` (live hop-list length), `ds_hop_visited`
(entries the capture helper really tunes: N / gcd(N, shuffle stride)) and
`ds_hop_ok` (1 = the live list equals the `channels=` list of the `source=` line in
`KISMET_SITE_CONF` as an unordered multiset and is fully visited; 0 = degraded;
NULL = no explicit list configured). Kismet 2025-09 can lose channel coverage
without the packet counter noticing - after a capture-helper crash it re-opens the
source with a collapsed list, and its hop stride can skip entries
(`hop_coverage.py`, `docs/findings.md` sections 7 and 9). `sensor/hop_guard.py`
repairs the collapse; these columns make any degraded window visible in the
data. Migration as for v3: `ALTER TABLE ADD COLUMN`, older polls read NULL; to
roll the code back set `meta.schema_version` to `3`.

Schema v3 added `observations.disconnects_last` (Kismet
`dot11.device.client_disconnects_last`, verified in `phy_80211.cc`: set to the
frame's unix second on every deauth/disassoc frame for the BSSID). A change of
it between two polls is an exact "deauth/disassoc frames arrived in between" bit,
whatever the burst size, whereas `disconnects` is only the current burst size
(restarts at 1 after a 1 s pause and after every DEAUTHFLOOD alert). A v2 buffer
is migrated at start by one `ALTER TABLE ADD COLUMN` (metadata only, instant, no
data loss; older rows read NULL). The old collector refuses a v3 buffer; to roll
the code back, set `meta.schema_version` to `2` - the extra column is harmless.
`kismet.device.base.num_alerts` is fetched but not stored: nothing in Kismet
increments it (0 on every device in every log, alerts included).

Schema v2 (associations deduplicated) replaced v1, where `associations` held one
row per poll and made up ~85 % of the buffer (~290 MB/day). A v1 buffer is
migrated automatically at start: the old table is dropped (not condensed - a
GROUP BY over millions of rows would stall the collector on the Pi) and recreated.
The dropped pages go to SQLite's freelist, so the file stops growing but does not
shrink; to reclaim the space once, with the collector stopped and free disk space
of at least the DB size (the `sqlite3` CLI is not installed on Pi OS Lite, so use
Python's built-in module):

```
sudo systemctl stop wifi-sensor-collector
sudo -u pi python3 -c "import sqlite3; sqlite3.connect('/var/lib/wifi-sensor/buffer.db').execute('VACUUM')"
sudo systemctl start wifi-sensor-collector
```

Future: `sent` on the deduplicated tables resets whenever `last_seen` advances
(every poll for an active pair) - settle that before building the upload step.
The deduplicated tables (`associations`, `probes`, `devices`) are never pruned;
a long-running sensor will eventually need a `last_seen`-based retention for them.

Read-only reports over the buffer (overview, RSSI stability, AP inventory churn)
live in `../analysis/` - see `analysis/README.md`.

Useful read-only queries on the Pi (`sqlite3 -readonly /var/lib/wifi-sensor/buffer.db`):

```sql
SELECT * FROM polls ORDER BY id DESC LIMIT 5;
SELECT d.ssid, o.ts, o.rssi FROM observations o JOIN devices d USING(key)
  WHERE d.ssid = 'MySSID' ORDER BY o.ts;
SELECT key, ts, crypt, adv_channel FROM device_config_history ORDER BY ts DESC LIMIT 10;
SELECT header, COUNT(*) FROM alerts GROUP BY header;
```

## Kismet facts the code relies on (verified on Kismet 2025.09.0)

- Field-filtered responses return the integer `0` for a field that does not
  exist on a device (never `null`). `shape.py` maps `0` to `NULL` for every
  non-counter field. In particular an RSSI of `0` is "no reading", never 0 dBm.
- There are no separate `wpa_version` / pairwise-cipher / RSN fields; the
  advertised crypto is `dot11.advertisedssid.crypt_string` plus the 64-bit
  `crypt_bitfield`. Both are stored.
- `ietag_checksum` and `beacon_fingerprint` vary between beacons (TIM/QBSS IEs
  change constantly), so they were per-poll observation values, never part of
  the configuration diff; not requested from v6 on.
- `kismet.device.base.channel` is the last *known* channel and can disagree
  with `frequency` on clients (neither is requested from v6 on). `frequency`
  (stored as `freq_khz` until v5) is the
  frequency Kismet attributed to the frame that last updated the device's
  frequency (`devicetracker.cc`: the frame's own channel information if any,
  else the tuned channel) - a different subset of frames than the one that sets
  the signal. It is **not** the reception channel of `rssi` and not reliably the
  AP's channel (a channel-12 BSSID showed 2417, 2437 and 5260 MHz); for the AP's
  channel use `devices.adv_channel`.
- An AP's `signal.last_signal` comes from a beacon
  (`dot11_ap_signal_from_beacon=true` in the packaged `kismet_80211.conf`) and
  Kismet takes the first radiotap dBm_AntSignal field; on 2.4 GHz that is a
  1 Mbps CCK beacon, hence the -106 dBm floor above.
- `/alerts/alerts.json` does not exist; `/alerts/last-time/{ts}/alerts.json` does.
  No alert had fired while this was written, so alert field *types* are handled
  leniently and the full alert JSON is kept in `alerts.raw`.

## Deliberately not collected

`dot11.client.ipdata` (DHCP/ARP-learned client IP addresses) and the WPS
identity fields (`wps_serial_number`, `wps_model_name`, `wps_manuf`,
`wps_device_name`) are personal data of bystanders captured without consent
and are not needed for the RF analysis. They are not requested from Kismet at
all. Should they ever be needed under explicit consent, `ipdata` would attach
as nullable columns on `associations` (it is a per-client-per-BSSID record in
Kismet) and the WPS fields on `devices`.
