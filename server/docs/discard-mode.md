# Discard mode (schema 20)

A per-sensor switch for tests away from the sensor's normal place (first use:
measuring the Pi's PoE power draw elsewhere). While it is on, the sensor keeps
uploading exactly as usual and the server keeps answering, but nothing of
the data it sends is stored. The time it covers is recorded as an **excluded
window**: never an outage, never lost polls, never coverage, no events.

Nothing changes on the Pi: no setting, no restart, no new code. The uploader
sees ordinary `200` answers and moves its cursor on, so after the test it
does not upload the test's polls again.

## Turning it on and off

**Settings -> Sensors -> Discard mode** (write token unlocked):

- **Turn on…** - a required note (what the test is) and an end: after 1-24 h
  (default 6 h) or at a given local time, at most 24 h ahead. It switches
  itself off at that end.
- **Turn off now** - stops it at once; from the sensor's next upload its data
  is stored again.

While it is on, a banner under the top bar of every page (the dashboard and
the Sensors page included) names the sensor, the end, the time left, the
polls discarded so far and the note, with a link back to the card. The
sensor's status reads **Discard mode · not stored** instead of online /
not capturing (unreachable stays unreachable).

The same through the api (the write token from the server's `.env`):

```
curl -X POST -H "Authorization: Bearer $WRITE_TOKEN" -H "Content-Type: application/json" \
     -d '{"note": "PoE power measurement in the lab", "duration_h": 6}' \
     https://34-42-184-97.sslip.io/api/v2/settings/sensors/pi-fri/discard
curl -X DELETE -H "Authorization: Bearer $WRITE_TOKEN" \
     https://34-42-184-97.sslip.io/api/v2/settings/sensors/pi-fri/discard
```

`until` (an ISO time with a zone) can replace `duration_h`. A second start
while one is on is a 409, so is a stop when none is on. Both writes add an
`audit_log` row (`sensor.discard.start` / `.stop`); the automatic end adds
`sensor.discard.expire`.

## What happens while it is on

`POST /api/ingest/v1/batch` authenticates, rate-limits and validates the
batch as always (an invalid batch is still a 422 and still a stall), then:

| Stored | Not stored |
|---|---|
| the heartbeat: history row, `sensor_status` (contact, last batch), adapter check, configuration report, retention check | polls, observations, devices, configuration history, alerts, AP load, frequency histogram |
| one `sensor_discarded_batches` row per batch: polls, buffer seq range, time range, rows per table, bytes (a batch sent again is the same row, `replays` + 1) | the `sensor_uploads` row, clock corrections (`sensor_clock_boots`), unverified gaps |
| `lost` gaps (polls pruned before the upload - older than the window, a real loss) | |

The answer has the usual keys (`inserted` empty) plus
`"discarded": {"until", "polls"}`. The standalone heartbeat endpoint is
unchanged. Nothing is marked for the refresher, so no rollup runs.

The window is "on" from `started_at` until `until` exactly
(`discard_active()`); the refresher's step 0 (`discard_close_expired()`,
every round) then records the end: `ended_at = until`, `ended_by =
'expiry'`, the history entry's end, the audit row and `write_version + 1`
(the banner goes at the next refetch).

## Where the window is excluded

The excluded time of a window (`discard_intervals()`) runs from the earlier
of its start and its first discarded poll (a backlog older than the start
that arrived while it was on) to the later of its end and its last discarded
poll; `sensor_excluded()` merges a sensor's windows.

- **Configuration history** - an `excluded` entry of `sensor_annotations`
  (from the start to the end, not a data boundary; label `Discard mode:
  <note>`); Settings shows it with its end, the polls discarded and their seq
  range (`discard_window_totals`). `evaluation/dataset/boundaries.csv` lists it.
- **sensor_periods** (`refresh_sensor_periods()`) - polls inside a window are
  not read; a mark at the window's start ends the state before it (a real gap
  *before* the window is an outage up to its start) and one at its end starts
  the next (no poll for more than 90 s *after* the window, e.g. the sensor
  still off on its way back, is an outage from the window's end). Sensor
  outage events come from `sensor_periods`, so none comes from a window; no
  Kismet, configuration or detector event can either - none of its data exists.
- **Loss / seq gaps** - `discarded_seqs()`: of a seq jump between two stored
  polls, the seqs a discarded batch covers are excluded, the rest stays loss.
  `evaluation/pipeline` gaps.csv (`excluded_polls`, reason `excluded`) and
  summary.csv (`seq_missing` without them, `seq_excluded`) use it. The Sensors
  page's lost / unverified counts never include a window (no gap row is
  stored for it).
- **Coverage** - the Sensors page: per day a grey *excluded* share instead of
  outage or "no data yet"; the 7-day coverage leaves the window out of its
  denominator. The period charts (Overview, Networks) show no data for those
  hours, as for any time without data. `evaluation/dataset` period.csv:
  `coverage` and `polls_expected` without the excluded time, `excluded_days`.
- **Pipeline export** (`evaluation/pipeline`) - `excluded.csv` lists the
  windows; contact availability (daily and summary) counts their time neither
  as observed nor as no contact (`excluded_s`); heartbeats.csv marks the
  window's heartbeats `excluded`, and summary.csv leaves them out of the clock
  figures; `batches_discarded`, `polls_discarded`.
- **Later rows** - `ingest_merge()` step 0, for the live ingest and the
  snapshot import alike: staged rows whose time lies inside an excluded window
  are dropped and recorded with the window as `late` rows - a replay of a
  discarded batch after the end, a backlog of the test that arrives only after
  the window ended (the test location had no uplink), a snapshot import that
  covers the window. A device or client row last seen inside a window is the
  window's; one first seen inside it and seen again after it starts at the
  window's end.
- **Resume point** - `ingest_resume()` (the heartbeat's `resume`): the newest
  of the stored polls and the discarded batches, so an uploader that lost its
  state never sends the window again.

## Limits (deliberate)

- **The end decides.** Data after `until` is stored again: if the sensor is
  still away then, its data lands in the dataset. Choose the end generously
  or turn it off by hand once the sensor is back.
- **The hour of the end loses its AP load.** An `ap_load_hourly` row that
  overlaps a window is dropped (the sensor aggregates per hour and would mix
  the test's polls in), so the load of the hours at either edge of a window is
  missing.
- **`device_freq_hist` is cumulative on the sensor.** An AP seen both at the
  test place and at home keeps the packets counted during the window in its
  histogram (only the band fallback of `ap_band()` reads it).
- **No configuration change during a window.** A configuration applied while
  the window is on is never confirmed: its effective poll is discarded.
- **Moving the sensor and putting it back** is not a hardware change by
  itself; if its antenna or position changed, add one (Settings -> Sensors ->
  Capture hardware).
- **Rollback** (`ops/rollback_schema20.sql`) refuses while a window exists.

## Code

`schema.sql` (schema 20; `ops/migrate_schema20.sh`): the tables
`sensor_discard_windows` and `sensor_discarded_batches`, the view
`discard_window_totals`, `discard_start/stop/active/close/close_expired()`,
`discard_intervals()`, `sensor_excluded()`, `discarded_seqs()`,
`ingest_resume()`, `refresh_sensor_periods()`, `ingest_merge()` step 0.
api: `app/ingest/discard.py` + `routes.py` (the ingest), `app/v2/writes.py`
`parse_discard()` and `routes/settings.py` (the toggle), `routes/sensors.py`
+ `shape.py` (status, coverage, outages), `app/refresher.py` step 0.
Frontend: `app/DiscardBanner.jsx`, `pages/settings/DiscardCard.jsx`, the
coverage and status helpers. Tests: `tests/sql/discard_test.sql` (fixture:
start/stop, expiry, windows, periods, loss, resume, late rows),
`api/tests/test_discard.py`, the frontend's `*.test.js`.

## Rollout record

2026-11-12, by Claude Code (the sensor was off from 07:05 UTC, so no upload
was in flight):

- Tests on the Mac: api 177, server 39 (schema files: the migration carries
  schema.sql's tables and functions), evaluation 71, frontend logic files
  under jsc; the frontend image build ran `node --test` (14 files) on the box.
- Rehearsal on `wifi_audit_scratch` (restore of `wifi_audit-20261112T074057Z.dump`):
  `MIGRATE_DB=wifi_audit_scratch sh ops/migrate_schema20.sh` - every fixture
  test PASS, `discard_test.sql` included; then an end-to-end run of the real
  ingest and Settings routes in a one-off api container against scratch (35
  checks: a normal batch stored; on via the route; a batch while on 200 with
  nothing stored, its record, a replay counted, a 422 still a 422; off via the
  route; a late batch's poll inside the window dropped and recorded, the new
  one stored; resume, the loss rule, no outage across the window, the
  outages row, coverage without an outage; the expiry by the refresher's step
  0 with its audit row and write_version). Two findings fixed before the live
  run: the history entry is linked by its ref, and the coverage widens a
  window to the adjacent polls (a poll spacing of 90 s or less is no outage).
  The pipeline and dataset export SQL ran on scratch (an e2e sensor: jump
  fully `excluded`, `seq_missing` 0; pi-fri: the same figures as before).
- Backup `wifi_audit-20261112T080953Z.dump` (sha256 a25012a9...b78c, equal on
  the box and in `~/wifi-audit-backups/`), then `sh ops/migrate_schema20.sh`
  08:13:45 - 08:14:14 UTC (log `~/migrate20-20261112T0813Z.log`): every fixture
  test PASS, the 23 stored sensor_periods equal to a recompute by the new
  function.
- `docker compose build api frontend`, `up -d api refresher frontend`
  08:18 UTC; `v2_smoke.py` 0 failures (130 checks, the new writes refuse
  without the token); `/api/health` schema 20; the changed read queries
  under 1 ms (`ep_v2.py`).
