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
| `../wifi-sensor-collector.service` | systemd unit template |
| `../install_collector.sh` | Installer, run on the Pi with `sudo` |

## Installation on the Pi

```
sudo ./install_collector.sh
```

Copies the code to `/opt/wifi-sensor/collector`, creates
`/etc/wifi-sensor/collector.conf` from the example (kept on re-runs), creates
`/var/lib/wifi-sensor` for the buffer, and enables `wifi-sensor-collector.service`
running as the sensor user (group `kismet`), started after `kismet.service`.
Credentials are read from Kismet's own `~/.kismet/kismet_httpd.conf`; nothing is
stored twice. Re-run the installer after every code change.

```
journalctl -u wifi-sensor-collector -f
INFO poll ok: total=237 active=94 new_obs=61 alerts=0/0 ds=up 210ms
```

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
5. Once per hour, delete `observations`, `associations` and `polls` older than
   `RETENTION_DAYS`.

A failed poll (Kismet restarting, datasource error) is recorded in `polls`
with `ok = 0` and the loop continues; the service is never taken down by it.

## Buffer schema (SQLite, `DB_PATH`)

| Table | One row per | Contents |
|---|---|---|
| `polls` | poll | collector/Kismet timestamps, device counts, datasource state, duration, error |
| `devices` | device | key, MAC, type, manufacturer, first/last seen; for APs the advertised configuration: SSID, cloaked, `crypt` (Kismet crypt string), `crypt_bits`, MFP supported/required, advertised channel, HT mode, beacon rate, country |
| `device_config_history` | AP configuration change | the configuration that was replaced, with the poll time of the change |
| `observations` | active device per poll | Kismet `last_time`, heard frequency and channel, RSSI last/min/max, cumulative packet and byte counters; AP only: associated client count, `disconnects` (deauth/disassoc seen), QBSS station count and channel utilisation, BSS timestamp (uptime), beacon IE checksum and fingerprint; client only: BSSID |
| `device_freq_hist` | device × frequency | cumulative packet count per frequency (Kismet `freq_khz_map`) |
| `associations` | poll × AP × client MAC | client MACs listed in the AP's associated-client map |
| `probes` | client × SSID | first/last time the SSID was probed for (`""` = wildcard) |
| `alerts` | Kismet alert | header, class, severity, MACs, channel, text, full JSON; deduplicated by Kismet's hash |
| `meta` | key | schema version, resume point |

Cumulative counters are stored as Kismet reports them; deltas are derived when
the data is analysed, which keeps the collector free of state and robust to
missed polls. `sent` columns exist for the later upload step.

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
  change constantly), so they are per-poll observation values, not part of the
  configuration diff.
- `kismet.device.base.channel` is the last *known* channel and can disagree
  with `frequency` on clients; `frequency` is stored as the heard frequency.
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
