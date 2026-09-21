# server/detection - batch detectors (prototype)

On-demand detectors over the data already in PostgreSQL. A detector reads a time
window of the source tables (`observations`, `alerts`, `devices`, `ap_baselines`,
the `ap_inventory` view - never written), builds per-AP episodes and writes one
row per episode into `detections`, the only table it writes. Nothing here is
scheduled or wired to ingest: the purpose is to develop and eyeball an algorithm
against real data - the seeded window now, a window with a staged attack later.
The Pi collector stays dumb.

```
docker compose run --rm detect deauth-flood --from 2026-09-15 --to 2026-09-21 --dry-run --verbose
docker compose run --rm detect deauth-flood --from 2026-09-15 --to 2026-09-21            # writes
docker compose run --rm detect deauth-flood --help
```

`detect` is a compose service behind the `tools` profile (never started by
`up`); it reuses the built `api` image and bind-mounts this folder, so a code
change needs no rebuild. Without containers: `cd server && DATABASE_URL=...
python3 -m detection deauth-flood ...` (needs psycopg 3). Apply `schema.sql`
once before the first run (it adds the `detections_dedupe` index):
`docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < schema.sql`.

Unit tests (stdlib, no database): `cd server && python3 -m unittest discover -s detection/tests`.

The dashboard shows the rows read-only: `GET /api/detections` and the *Detections*
page (row expand = the evidence); Overview carries an "open detections" tile.

| File | Purpose |
|---|---|
| `__main__.py` | `python -m detection <detector> [options]`; one subcommand per detector |
| `common.py` | connection from `DATABASE_URL`, sensor resolution, `--from/--to` parsing, table printer |
| `detections.py` | the sink: matches an episode to its stored row and inserts or refreshes it |
| `deauth_flood.py` | the deauth/disassoc flood detector (below) |
| `tests/` | synthetic-timeline tests of the pure logic |

Every detector runs its analysis in a `READ ONLY` transaction and its writes in
a second transaction that touches only `detections`; `--dry-run` skips the
second one.

## Idempotency

A finding is matched to a stored row of the same sensor, detector type and
subject (the AP's `device_key`, or its upper-case BSSID when no device row
exists) whose stored window (`evidence.window_start/window_end`) overlaps the
new episode widened by the detector's episode gap. A match is refreshed in place
- `ts`, `severity`, `summary`, `evidence`, identity - otherwise a row is
inserted. So re-running over the same, a shifted or an overlapping window never
duplicates an episode, and re-running with other thresholds re-grades the stored
episode (latest run wins). Never touched on a refresh: `id`, `acked`, `acked_at`,
`ack_note`, `created_at`. The evidence carries `runs`, `first_run_at` and
`last_run_at` across refreshes. `schema.sql` adds a unique index on
`(sensor_id, type, coalesce(device_key, upper(mac::text)), ts)` as the guarantee
for an exact re-run.

## deauth_flood

### What the data actually carries

`observations.disconnects` is Kismet's `dot11.device.client_disconnects`. It is
**not a cumulative counter** (Kismet `phy_80211.cc`):

```c
if (now - client_disconnects_last > 1) client_disconnects = 1;
else                                   client_disconnects += 1;
client_disconnects_last = now;
if (client_disconnects > 10) { raise DEAUTHFLOOD; client_disconnects = 1; }
```

It is the size of the *current* burst of deauth/disassoc frames for that BSSID
(frames with no gap > 1 s between them), never above 11, restarting at 1 after a
pause and after every alert. During a flood its polled value is essentially
random in 1..11, so deltas mean nothing and a median/MAD baseline of them is
degenerate. Verified on the seeded 5 days (885 143 AP polls, 343 APs): the
maximum value ever polled is 6, it decreases on ordinary polls (2->1, 5->2), it
drops to 0 for every AP at a Kismet restart, and of the two organic floods
Kismet alerted on, one moved it 1->4 and the other not at all (2->2).

What a poll does tell: a value that **changed to a non-zero number** since the
previous poll of the same AP means at least one new burst happened in between
(a *burst event*; a drop to 0 is a device re-creation). It is a lower bound - one
event per poll whatever happened in the 30 s - but organically it is rare and
isolated: 76 events on 28 APs in 5 days, never on two consecutive polls, never
more than 2 per AP within an hour. A flood changes the value in about 10 of 11
polls.

The `DEAUTHFLOOD` alert (Kismet's `> 10` rule above) is throttled on the Pi by
`alert=DEAUTHFLOOD,5/min,2/sec`: an organic one-second storm gives exactly a pair
of alerts within a second and nothing more; a sustained flood keeps producing
them at 5 per minute for its whole duration, so the *span* of the alerts tells
how long the flood lasted. `alerts.device_key` is `00_0` for these alerts; the
AP is joined by `alerts.transmitter_mac` (= the BSSID). The frame direction comes
from the addresses: `dest = ff:ff:ff:ff:ff:ff` -> `broadcast`, `source = bssid`
-> `from_ap`, `dest = bssid` -> `to_ap`. Both organic events were `to_ap` (a
client storm); a spoofing attacker produces `from_ap` / `broadcast`.

### Algorithm

Per sensor and AP, over `[--from, --to)` widened by `--gap` on both sides:

1. **Events**: `DEAUTHFLOOD` alerts (trigger), `BCASTDISCON` and
   `DISCONCODEINVALID` alerts (corroborators only), burst events.
2. **Episodes**: events of one AP merged in time order; a silence longer than
   `--gap` closes an episode.
3. **Rules** - an episode is a detection when
   - **A.** it holds at least one `DEAUTHFLOOD` alert (Kismet's rule *is* the
     flood definition; the server does not second-guess it), or
   - **B.** it holds >= `T` burst events within a sliding `--burst-window`, with
     `T = max(--min-bursts, q)` where `q` is the smallest count whose Poisson
     tail probability, at the AP's own organic burst rate (bursts per active
     hour learned over `--lookback`, the per-AP baseline), is below `--alpha`.
     For every AP in the seeded data the rate is <= ~0.1/h, so the floor of 3
     decides; the baseline matters for busier deployments and is always in the
     evidence.
4. **Severity**

   | condition | severity |
   |---|---|
   | A with alert span < 2 s (one burst-second, the organic pattern), B false | `low` |
   | A with span 2 s .. `--sustain`, or B alone | `medium` |
   | A with span >= `--sustain` (alerts in two throttle minutes), or A and B together | `high` |
   | one level up when the frames come from the AP side (`from_ap`/`broadcast`) on an AP with `ap_baselines.trusted` | |

5. **Evidence only, not a trigger**: the AP's non-data frame rate per poll,
   `d(pk_total - pk_data)`, with its median + 1.4826*MAD baseline over the
   lookback (the estimator `refresh_ap_baselines()` uses) and the episode's
   max/mean. A spoofed-from-AP flood should inflate it by orders of magnitude
   (organic: ~10 frames per 30 s poll for the campus APs, robust sd ~1.5-3);
   the staged attack decides whether it earns trigger status.

### Options and defaults

| flag | default | meaning |
|---|---|---|
| `--from`, `--to` | `--to` = now, `--from` = 24 h earlier | window, ISO 8601, UTC when no offset, `--to` exclusive |
| `--sensor` | first sensor | `sensors.name` |
| `--gap` | 300 s | silence that closes an episode (a flood produces an alert at least every ~12 s) |
| `--max-gap` | 120 s | a counter change counts only if the previous poll is this close (localisable) |
| `--min-bursts` | 3 | rule B floor (organic maximum: 2 per AP per hour) |
| `--burst-window` | 600 s | sliding window for rule B |
| `--alpha` | 1e-4 | Poisson tail for the per-AP threshold above the floor |
| `--sustain` | 60 s | alert span that makes an episode `high` |
| `--lookback` | 7d | baseline window before `--from`; APs with < 24 h of it fall back to the scan range (marked in the evidence) |
| `--headers` | `DEAUTHFLOOD` | trigger headers |
| `--dry-run`, `--verbose`, `--json` | | write nothing / print every episode's timeline / findings as JSON on stdout |

### The row

`type = 'deauth_flood'`, `ts` = episode start (first alert or burst poll; a burst
poll is up to 30 s after its frames), `severity`, `device_key` (NULL when the
BSSID has no device row), `mac` = BSSID, `ssid`, `summary`, `evidence`:

```
window_start, window_end, duration_s, rules {alerts, bursts},
alerts {ids, n, span_s, first_ts, last_ts, by_header, sources, directions, broadcast,
        corroborating {BCASTDISCON: [ids], DISCONCODEINVALID: [ids]}},
bursts {n, polls, values [[prev, cur]...], max_in_window, threshold},
baseline {from, to, fallback, active_hours, polls, bursts, bursts_per_hour, flood_alerts,
          mgmt_per_poll_median, mgmt_per_poll_robust_sd},
observed {bursts_per_hour, ratio_to_baseline, mgmt_per_poll_max, mgmt_per_poll_mean,
          mgmt_ratio_to_median, polls},
ap {bssid, ssid, crypt, mfp_req, trusted, device_key}, params, runs, first_run_at, last_run_at
```

### Validation

**Seeded window** (`--from 2026-09-15 --to 2026-09-21`): expected exactly two
detections, both `low`, both rule A, both `to_ap`:

| start (UTC) | AP | alerts | burst polls |
|---|---|---|---|
| 2026-09-18 07:42:56 | `EC:58:EA:55:89:DC` IK-WIFI | 12, 13 (13 ms apart) | 1 (counter 1 -> 4) |
| 2026-09-20 09:01:36 | `EC:58:EA:54:45:8C` IK-WIFI | 25, 26 (40 ms apart) | 0 (counter 2 -> 2) |

Rule B alone: 0 rows. `--dry-run --min-bursts 2 --burst-window 3600` shows the
organic near-misses the default sits above. Idempotency: re-run the same window
(still 2 rows, `evidence->>'runs'` = 2), run two halves split at 2026-09-18 12:00
and then overlapping windows (still 2 rows), acknowledge one row and re-run
(`acked` kept). Row counts of every source table are unchanged by construction.

**Staged attack** (later, against the developer's own test AP only; note the
start/stop times): (a) `aireplay-ng --deauth 0 -a <test AP>` (broadcast, from
the AP), (b) the same with `-c <own client>` (unicast, both directions), (c) a
slow variant (one deauth every 2-3 s) that stays under Kismet's > 10 rule, each
>= 3 min. The sensor channel-hops; consider locking the datasource to the test
AP's channel for the experiment. Then run the detector over a window holding the
runs and record per run: detected, latency (`ts` - attack start), severity,
directions and corroborators, number of rows (one per run unless paused longer
than `--gap`), `mgmt_ratio_to_median`; and over the whole window the rows on any
other AP (false positives), giving precision, recall over the staged runs and
the contribution of rule A vs rule B. Expected: (a) and (b) one `high` row each
with `DEAUTHFLOOD` at 5/min (+ `BCASTDISCON` for (a)) and bursts in ~10 of 11
polls; (c) most likely missed by both signals (see below). Sweep `--min-bursts`,
`--burst-window` and `--gap` in `--dry-run` over both windows to pick the
defaults for the thesis and to produce the FP/TP table.

### Limitations

- Sensitivity is bounded by Kismet's counter: an attack with > 1 s between
  frames never alerts and, if its bursts are of constant size, never changes the
  counter either.
- Burst events are a lower bound: one per poll, whatever happened in the 30 s.
- Both organic events are client-side storms Kismet still calls floods; they are
  reported as `low`, not suppressed.
- Kismet keeps `alertbacklog=50` alerts; at the throttled 5+5 per minute a flood
  fills it in ~5 min, and the collector polls every 30 s, so nothing is lost.
- The per-AP baseline query reads every poll of the AP over the lookback
  (~10 s per AP pair on a cold cache, since one AP's rows are spread over the
  whole table); the event scan is one pass over the window (~10 s for 5 days).
- Recommended collector follow-up (a separate sensor change, out of scope here):
  also sample `dot11.device.client_disconnects_last` (unix second of the last
  burst) and store the already-fetched per-device `num_alerts`. A change of
  `_last` between polls is an exact "deauth activity in this poll" bit
  independent of burst size, which turns signal 2 into a proper rate.
