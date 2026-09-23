# wifi-sensor

The Raspberry Pi half of the `wifi-audit` project: a **fixed, passive Wi-Fi
monitoring sensor**. It observes the wireless environment continuously and
records compact time series (RSSI, appearance/disappearance, configuration
changes, Kismet WIDS alerts, probe requests) into a local buffer. It never
transmits, deauthenticates, captures handshakes or cracks anything.

```
USB adapter (wlan1, monitor mode)
   -> Kismet (capture, device tracking, WIDS alerts)
   -> collector.py, every 30 s over Kismet's REST API
   -> SQLite buffer /var/lib/wifi-sensor/buffer.db (14-day retention)
   -> analysis/ scripts (read-only, by hand)   [-> server/, later]
```

## Hardware and OS

- Raspberry Pi 3B+ (1 GB RAM), Raspberry Pi OS 64-bit Lite, headless.
- Capture: an external USB adapter with a mainline monitor-mode driver
  (in use: RTL8821CU, `rtw88_8821cu`, as `wlan1`). Chosen by `CAPTURE_IFACE`,
  never hardcoded.
- On-board Wi-Fi / Ethernet are for management only; the installer refuses to
  touch the interface carrying SSH or the default route.

## Layout

| Path | Purpose |
|---|---|
| `install_sensor.sh` | Provisions Kismet from its apt repo, chrony, the unprivileged `kismet.service` override, the pre-start, watchdog and log-retention helpers, and a journald size cap. Run first. |
| `install_collector.sh` | Installs the collector daemon and the analysis scripts, creates the config and buffer directory, enables `wifi-sensor-collector.service`. Run second. |
| `sensor.conf.example` | Installer configuration template (copy to `sensor.conf`, gitignored). |
| `sensor/` | Kismet-side helpers: `kismet-prestart.sh` (USB/clock wait, stale monitor VIF cleanup, log retention), `capture-watchdog.sh` (restarts Kismet / reloads the driver / re-plugs USB when no frames arrive) and `kismet-log-retention.sh` (hourly + every Kismet start: deletes old / empty / over-cap kismetdb logs, never the open one). |
| `collector/` | The deployed daemon (`collector.py`, `kismet_client.py`, `shape.py`, `store.py`), its config template, tests and [README](collector/README.md) with the buffer schema. |
| `systemd/` | Unit templates rendered by the installers. |
| `analysis/` | One-off, read-only reports over the buffer; not part of the service. See its [README](analysis/README.md). |
| `docs/` | [findings.md](docs/findings.md): headline numbers measured so far. |
| `CLAUDE.md` | Working context and rules for AI-assisted development of this folder. |

Installed on the Pi as `/opt/wifi-sensor/{sensor,collector,analysis}`, config in
`/etc/wifi-sensor/collector.conf`, buffer in `/var/lib/wifi-sensor/`.

## Install and update on the Pi

```bash
git clone <this repository> ~/wifi-audit      # once
cd ~/wifi-audit/wifi-sensor
cp sensor.conf.example sensor.conf            # set CAPTURE_IFACE=wlan1 etc.
sudo ./install_sensor.sh                      # prompts for the Kismet web login the first time
sudo ./install_collector.sh
```

Both scripts are idempotent: after `git pull`, re-run them from the same folder
to deploy changes. They copy, configure and (re)start; they never delete files
on the Pi. The one runtime exception is `kismet-log-retention.sh`, which prunes
Kismet's own `wifi-sensor-*.kismet` logs (default: 3 days / 1 GB / keep 2 GB free,
see `sensor.conf.example`). Kismet's REST credentials live in `~/.kismet/kismet_httpd.conf` and
are read from there by the collector - nothing is stored twice.

## Operating

```bash
systemctl status kismet wifi-sensor-collector
journalctl -u wifi-sensor-collector -f        # "poll ok: total=… active=… skipped=0 …"
systemctl list-timers wifi-sensor-capture-watchdog.timer wifi-sensor-kismet-log-retention.timer
journalctl -u wifi-sensor-kismet-log-retention  # which kismetdb logs were deleted
df -h / ; journalctl --disk-usage              # journal capped at 200 MB
iw dev                                        # expect wlan1mon, type monitor

python3 /opt/wifi-sensor/analysis/analyze.py  # buffer overview (read-only)
```

Runtime settings (poll interval, retention, paths) are in
`/etc/wifi-sensor/collector.conf`; edit, then
`sudo systemctl restart wifi-sensor-collector`.

## Development (on the PC)

```bash
cd wifi-sensor
python3 -m unittest discover -s collector/tests     # stdlib only
bash -n install_sensor.sh install_collector.sh
```

## Privacy

The sensor is passive-only and deliberately does not collect bystander PII:
no client IP data and no WPS device identity fields are requested from Kismet.
Client-AP associations and probed SSIDs are stored as behaviour over time,
without those identifiers. Details: `collector/README.md`, "Deliberately not
collected".
