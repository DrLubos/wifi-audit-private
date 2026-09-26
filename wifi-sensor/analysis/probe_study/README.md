# Probe-request feasibility study (scripts)

Tools used for the feasibility study of passive probe-request fingerprinting and
re-linking of randomised MACs (results: `../../docs/findings.md`, section 8).
They are **not** part of the deployed sensor: `install_collector.sh` copies only
`analysis/*.py`, not this folder. Run them by hand on the Pi from the repository
clone (e.g. `~/wifi-audit-private/wifi-sensor/analysis/probe_study`).

Stdlib only. Kismet access works like the collector's: `KISMET_URL` (default
`http://127.0.0.1:2501`) and `KISMET_AUTH_FILE` (default
`~/.kismet/kismet_httpd.conf`); credentials go into an HTTP header, never onto a
command line. The Kismet stream needs only the read-only role; nothing here
needs root except `raw_vs_kismet.py`.

## Privacy rules (hard)

- Passive only. Nothing here transmits; the raw socket is receive-only.
- Raw frames exist only in the capture file written by `probe_capture.py`,
  which must be on tmpfs (`/tmp` on the Pi; other paths are refused). Delete it
  after the analysis. Never copy it off the Pi or into this repository.
- Output is aggregates only. MACs and SSIDs are handled as keyed hashes with a
  random per-process key that is never stored, so hashes cannot be linked
  across runs.
- No client IP data. WPS elements count only by presence, because they carry
  UUID-E and device names.

## Scripts

| Script | What it does |
|---|---|
| `probe_capture.py` | Reads Kismet's per-datasource pcapng stream, keeps only probe requests and writes them to a tmpfs pcapng. Prints stream counters (frames per type, stream vs datasource counter) and the capture cost (CPU of Kismet, of its capture helper and of itself; RSS; system CPU) |
| `probe_analyze.py` | Aggregate report on a capture: rates, randomised-MAC share, radiotap fields delivered, received channel vs DS Parameter channel, IE presence, IE order/content fingerprints (entropy, anonymity sets), sequence numbers, bursts, directed vs wildcard probes |
| `probe_linkage.py` | Follow-up: does the sequence number continue across a MAC change, how long randomised MACs live, and the largest fingerprint groups (e.g. one device rotating its MAC on every scan) |
| `hoplog.py` | Frames and "visits" per radiotap frequency in the stream, and the largest gaps without frames. A frequency change is not a hop (HT40/VHT80 dwells report several 20 MHz frequencies) |
| `dwell_poll.py` | The real hop schedule, from polling `iw dev <mon> info`: dwell per hop, stalls, distinct channel settings visited, 2.4 GHz time share |
| `raw_vs_kismet.py` | Root only. Counts probe requests on a raw socket (kernel BPF, receive only) and on the Kismet stream at the same time and compares them. Reports the socket's kernel drop counter and CPU per path |
| `buffer_hourly.py` | New probing client devices (i.e. MACs) per UTC hour over the last days, from the collector buffer, read-only |
| `probe_common.py` | Shared helpers (Kismet REST, pcapng, radiotap, IE parsing, keyed hashing, /proc) |

## Typical run

```
cd ~/wifi-audit-private/wifi-sensor/analysis/probe_study
mkdir -m 700 -p /tmp/probe_study
python3 probe_capture.py /tmp/probe_study/probes.pcapng --duration 900   # 15 min
python3 probe_analyze.py /tmp/probe_study/probes.pcapng
python3 probe_linkage.py /tmp/probe_study/probes.pcapng --top 3
rm -rf /tmp/probe_study                                                    # always

python3 dwell_poll.py wlan1mon --duration 90 --list
python3 hoplog.py --duration 180
python3 buffer_hourly.py --days 3
sudo python3 raw_vs_kismet.py wlan1mon --duration 300
```

Record the UTC time window of every run. It was night-time for the first study
(2026-09-26 22:01-22:16 UTC); repeat at the daytime peak before drawing
conclusions. The hop list also changed on 2026-09-26 (findings, section 7), so
per-channel numbers from before and after that are not directly comparable.
