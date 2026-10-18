# Pipeline metrics export (live upload, system-evaluation chapter)

`export.sh` writes the live-upload pipeline's metrics for one sensor and a
date range as CSV files. It is read-only: SQL as the read-only role
`claude_ro` inside the db container, plus the refresher's container log.
Pipeline design: `../../docs/live-upload.md` (section 11 lists the thesis
measurements).

```
# on the box, from server/
evaluation/pipeline/export.sh --from 2026-10-17T20:46:38Z --to 2026-10-25
evaluation/pipeline/export.sh --from 2026-10-18 --to 2026-10-19 --sensor pi-fri --out ~/exports/day1
# on the Mac
scp -r google:pipeline-exports/<folder> .
```

`--from` / `--to` are UTC (anything `date -d` reads); `--to` is exclusive.
`--gap-s` sets the contact-gap threshold (default 180 s, the same rule as
`refresh_contact_periods()`). `--refresher-log FILE` reads an archived log
instead of `docker compose logs`. The default output folder is
`~/pipeline-exports/<sensor>_<from>_<to>/`. `export.txt` records the
parameters and the git commit.

Which rows are in range: polls by their (corrected) `ts`, uploads and
heartbeats by `received_at`, recorded gaps by `recorded_at`.

## Files and how each metric is computed

### `polls.csv` – one row per poll uploaded live

The source is `polls` joined to the `sensor_uploads` row of its batch on
`(sensor_id, received_at)`. Both columns are set to the merge transaction's
`now()`, so they are equal; polls loaded by a snapshot import have no match
and are left out.

| Column | Meaning |
|---|---|
| `latency_s` | **Upload latency** = `received_at - ts`: from the poll's start (`ts`, on the true time line) to its commit on the server. |
| `queue_s` | `sent_at - ts`: waiting on the sensor. Covers the poll's own duration, the uploader's 2-5 s detection, backoff and catch-up queueing. `sent_at` is the sensor's corrected clock. |
| `transfer_s` | `received_at - sent_at`: network, TLS, server merge, plus any retry delays. Mixes the two clocks, so its error is the clock offset (see heartbeats). |
| `ts_offset_s` | The clock correction the uploader applied to `ts` (NULL = the clock was synchronised). |
| `batch_polls`, `mode` | Polls in the same batch. `steady` = a batch of one poll (normal operation). `backlog` = a catch-up or merged-after-retry batch. Report latency for `steady` separately: backlog latency is the outage length, not the pipeline's latency. |
| `attempt`, `retries`, `replays` | From the batch: the uploader's attempt number of the stored send, `attempt - 1`, and how often the stored batch arrived again. |
| `batch_bytes`, `merge_ms` | Gzip body size; server time of `ingest_merge()`. |

### `uploads.csv` – one row per stored live batch

- **Retries** = `attempt - 1`: failed sends of that batch before it was
  stored. Causes are 5xx while the db restarts, network loss and timeouts.
  The uploader keeps the attempt number.
- **Replays** = the same `(buffer_id, batch_seq)` arriving again after it
  was stored, typically because the response was lost. Nothing is inserted
  twice; only the counter moves.
- `polls_inserted` is 0 for a pure replay.
- Refused requests (401/413/422) are not stored as uploads. They show up as
  stall events (`detections` kind `sensor`) and in the uploader's journal on
  the Pi.

### `gaps.csv` – loss

Two independent sources, so they can be cross-checked:

- `seq_jump`: `polls.seq` is the buffer's `polls.id`, consecutive on the
  sensor. A jump between consecutive stored polls (ordered by `ts`) is
  `seq - prev - 1` polls that never arrived. A seq going down is a new
  buffer (`buffer_change`), not a loss.
- `recorded`: `sensor_seq_gaps`, written by the ingest. `lost` means pruned
  on the sensor before upload, or a gap the server found. `unverified` means
  a boot that ended without a clock anchor; those polls are never uploaded
  (`raw_from`/`raw_to` are on the sensor's wrong clock, indicative only).

**Loss rate** = `seq_missing / (seq_received + seq_missing)` (`summary.csv`).

### `heartbeats.csv` – one row per stored heartbeat (at most one a minute)

**Clock offset** (`clock_offset_s`) depends on `clock_method`. The method
changed on 2026-10-17 (`live-upload.md` section 17):

| `clock_method` | Until | Meaning |
|---|---|---|
| `recv_minus_send_minus_half_rtt` | 2026-10-17 21:08:13 | Server receive - sensor send - half the *previous* request's RTT. That RTT was mostly merge time, so the result was biased (about -1.2 s). |
| `recv_minus_send_early_stamp` | 2026-10-17 21:32:30 | Receive - send, but the sensor stamped before building the heartbeat. |
| `recv_minus_send` | now | Receive - send, stamped right before the POST. |

`recv_minus_send` = the clock offset **plus the one-way latency**, including
TCP+TLS setup: 0.35-0.4 s with both clocks within 1 ms of NTP. The Pi's own
NTP offset is `chrony_offset_s`; a heartbeat with `chrony_synced` false
belongs to a boot without NTP.

The other columns are the sensor's state per minute: backlog, held and
unverified polls, buffer size, free disk.

### `contact_gaps.csv`, `contact_daily.csv` – contact availability

- A **contact gap** is two consecutive heartbeats more than `gap_s` apart,
  clipped to the range. An open gap (no heartbeat since) runs to now.
- **Availability** (per UTC day, and over the range in `summary.csv`) =
  `1 - no_contact_s / observed_s`. Observed time starts at the sensor's
  first heartbeat and ends at the range end or now.
- "No contact" does not distinguish a sensor that is down from a network
  path that is down. The polls themselves arrive later (store-and-forward),
  so loss is measured separately, in `gaps.csv`.

### `refresh.csv` – refresher steps

Since schema 16 the refresher writes one `refresh_runs` row per step
(`api/app/refresher.py`); `source` = `db`. For times before the table's
first row, the export falls back to the refresher's container log
(`source` = `log`). That log is lost whenever the container is recreated:
for the first live days it starts at 2026-10-18 14:35, and
`refresh_first_row` in the summary says where the series begins.
`--refresher-log FILE` reads an archived log instead.

| `step` | Row |
|---|---|
| `rollups` | `refresh_rollups()` of one dirty sensor. `duration_s`; `refresh_from` = the dirty mark; `waited_s` = how long the oldest new data waited for a refresh; `ap_hours`/`sensor_hours` recomputed. |
| `contact_periods` | `refresh_contact_periods()` for all sensors (`sensor` empty). `gaps` = unreachable periods written. |
| `ap_baselines` | The nightly `refresh_ap_baselines()` (`baselines` = baselines learned). |
| `round` | The round's steps 1-2 (`duration_s`) and the delay to the next round (`next_in_s`; above the interval = the duty-cycle cap). In the log only capped rounds appear. |
| `error` | Log only: a database error (`ok` = f). In the table a failed step is its own row with `ok` = f and `error`. |

**Refresh duration** in `summary.csv` = p50/p95/max of the successful
`rollups` rows; `refresh_rollups_failed` counts the failed ones.

### `summary.csv` – one row per aggregate

Columns are `metric,value,unit,note`, covering everything above:

- polls, steady and backlog;
- latency p50/p95/p99/max (steady) and max (backlog);
- batches, retries and replays;
- bytes and merge time;
- seq received, missing and loss rate;
- recorded gaps;
- contact observed and availability;
- clock offset p50/p95 (current method only) and the chrony offset;
- refresh p50/p95/max and the nightly baseline max.

Percentiles use `percentile_cont` (interpolated) in SQL; the refresh ones
use the nearest rank.
