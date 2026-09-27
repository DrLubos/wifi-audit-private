# wifi-sensor analysis scripts

One-off, read-only reports over the collector's SQLite buffer. They are **not
part of the deployed service**: nothing here runs automatically, flags anything
live or writes to the buffer. `install_collector.sh` copies them to
`/opt/wifi-sensor/analysis` so they can be run by hand on the Pi; a change here
does not restart the collector.

Every script opens the buffer read-only so the collector keeps running. Run them
as the sensor user (it owns the `-shm` file). The buffer path is resolved the
same way the collector resolves it: positional `DB_PATH` argument, then the
`DB_PATH` environment variable, then `DB_PATH` from
`/etc/wifi-sensor/collector.conf` (or `COLLECTOR_CONF`), then
`/var/lib/wifi-sensor/buffer.db`. All scripts are stdlib only.

**Dataset rule - RSSI floors:** every script reads RSSI through
`../collector/dataset_rules.py` (found next to `analysis/` both in the repo and
under `/opt/wifi-sensor`): the adapter's floor values -106/-120 dBm are
censored readings, never signal levels, and are left out of every RSSI
statistic. Buffer rows written before schema v5 still carry them raw, so the
filter is applied on read. Figures published before 2026-09-27 include them
(`docs/findings.md` sections 2-3 and 11).

| File | Purpose |
|---|---|
| `analyze.py` | Summary of the buffer (totals, RSSI stability, config changes, alerts, probes, growth) |
| `rssi_stability.py` | RSSI baseline statistics across all APs (per-AP std dev distribution, day-to-day drift, out-of-baseline rate per threshold) |
| `inventory_changes.py` | Characterisation of AP inventory churn (baseline vs later, transient vs sustained, impostor candidates for protected SSIDs) |
| `probe_study/` | Probe-request feasibility study tools: live capture from Kismet's stream to tmpfs, fingerprint/linkage analysis, hop-dwell measurement. Not installed; run from the repo clone. See `probe_study/README.md` |

## analyze.py

A one-shot overview of what has been collected:

```
python3 /opt/wifi-sensor/analysis/analyze.py            # path from collector.conf
python3 /opt/wifi-sensor/analysis/analyze.py --top 12 --hist 5 --utc
```

## rssi_stability.py

Quantifies how stable a fixed AP's RSSI is as seen by the fixed sensor, across
every AP with enough readings (default: >= 200 readings on >= 2 days). It
reports the distribution of per-AP standard deviation (plain and MAD-robust, by
band and by signal strength), the day-to-day drift of each AP's mean, and - with
a baseline fitted on the first half of each AP's history and evaluated on the
second half - how often genuine readings exceed `|rssi - baseline| > Y dB` for a
sweep of thresholds, by how much, and whether they come as single samples or
runs. It describes the data only; nothing is flagged or stored.

```
python3 /opt/wifi-sensor/analysis/rssi_stability.py                 # full report
python3 /opt/wifi-sensor/analysis/rssi_stability.py --no-table --csv ~/rssi_aps.csv
python3 /opt/wifi-sensor/analysis/rssi_stability.py --min-obs 500 --min-days 3 --thresholds 4,6,8,10
python3 /opt/wifi-sensor/analysis/rssi_stability.py --until "2026-09-19 12:34" --max-floor-share 0.35
```

The dataset section reports the floor readings and the per-AP floor share
(floor readings / all readings). `--since` / `--until` (UTC, `YYYY-MM-DD[
HH:MM[:SS]]`) restrict the window; `--max-floor-share F` drops APs whose floor
share exceeds F (the evil_twin eligibility rule, 0.35 on the server) - dropping
floor readings biases a weak AP's median upward, so the per-AP figures of such
APs are not comparable; `--keep-floor` counts the floor values as levels again
(the pre-2026-09-27 behaviour, only to reproduce old figures).

## inventory_changes.py

Asks whether "a network appeared where it should not" is a usable signal. It
splits the capture into a baseline window (first 24 h) and the rest, reports APs
that appeared later or vanished, measures the raw churn (new APs per day,
transient vs sustained, share of randomised BSSIDs - the noise floor of a naive
"new AP" alert), and then looks at the targeted signal: for each `--protected`
SSID prefix every BSSID advertising it (now or earlier, via the config history)
with OUI, encryption, first/last seen and typical RSSI, flagged when the OUI
differs from the infrastructure (given with `--infra-oui` or inferred), it
appeared late, it is much stronger than its peers, uses a different encryption,
has a randomised BSSID or carried another SSID before; plus look-alike SSIDs. A
persistence x strength cross-table of all newcomers and a candidate list close
the report. Characterisation only - nothing is flagged live or stored.

```
python3 /opt/wifi-sensor/analysis/inventory_changes.py --protected IK-WIFI,FRI_wifi
python3 /opt/wifi-sensor/analysis/inventory_changes.py --protected IK-WIFI --infra-oui 00:11:22 --csv ~/candidates.csv
python3 /opt/wifi-sensor/analysis/inventory_changes.py --protected IK-WIFI --baseline-hours 48 --close-dbm -55 --list 50
```
