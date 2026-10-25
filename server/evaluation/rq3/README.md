# RQ3 – continuous monitoring vs one-shot audits (read-only simulation)

What would a one-shot audit - the prior work's model: an operator arrives,
captures for a while, leaves - have seen, compared with the continuous
record of the same sensor? The tool cuts **simulated audits** out of the
stored data: random windows of 15 min and 1 h, and pairs of 1-h audits a day
or a week apart (a repeat audit), and compares what falls inside them with
the whole continuous record: APs found, configuration changes,
security-level changes, events.

```
# on the box, from server/ (~12 min with the defaults; the db is read in small chunks)
evaluation/rq3/export.sh --from 2026-09-15 --to 2026-10-24
evaluation/rq3/export.sh --n 200 --durations 900,3600,14400 --pairs 100 --gaps 86400,604800 --seed 7
evaluation/rq3/export.sh --from 2026-09-15 --to 2026-10-24 --hours work     # weekdays 08:00-17:00 only
# on the Mac
scp -r google:rq3-exports/<folder> .
```

SQL runs as the read-only role `claude_ro` inside the db container; the
sampling and the summaries are `python3 -m evaluation.rq3` (standard
library). Tests: `python3 -m unittest discover -s evaluation/tests`
(`test_rq3.py`, runs on Python 3.9 too).

## Method

**The simulated audit.** An audit of length *d* starting at *t* sees exactly
what the continuous record holds inside [*t*, *t + d*): the same adapter,
hop list (89 channels, ~19 s per cycle) and position. So the comparison
isolates *time* - one window versus the whole record - and nothing else:
not the auditor's equipment, place or skill.

**Sampling.** Audit starts are drawn without replacement, uniformly over
**collecting time**: from the collecting polls (`ok`, datasource running,
devices > 0; one every 30 s), keeping only starts whose window ends before
the last poll. An audit never starts in an outage (the continuous record has
nothing there either), but a window can run into one - its `polls:collecting`
count shows it. The seed is fixed (`--seed`, default `20261024`); every
sampled audit is listed in `sampled.csv`, so a run can be checked by hand.
Repeat audits: *t1* drawn the same way, *t2* = the first collecting poll at
or after *t1 + gap*.

**Two variants** (`--hours`): `24h` - starts at any time of day and week;
`work` - the whole audit window inside **weekdays 08:00-17:00
Europe/Bratislava** (a one-shot audit as an operator would do it; DST
handled by the time zone, public holidays not modelled), and for a repeat
audit *t2* the first such collecting poll at or after *t1 + gap* (a 1-day
gap from a Friday lands on Monday). The continuous reference is the same
for both: the whole record, all hours - which is the comparison RQ3 asks
for. The 24h variant draws with the same seeds as before the variant
existed, so its runs are reproducible.

**The continuous reference** (`reference.csv`) is the record over
[`--from`, `--to`): every AP heard (an hour of `ap_rssi_hourly` with
observations), every row of `ap_config_changes`, every new period of
`ap_security_periods`, every event of kind detection / kismet / config.

**APs heard in a window**: an AP with at least one observation row in it
(a frame that Kismet counted as a new sighting; candidates from the hourly
rollup, then an index-only probe of `observations_pkey` - a ts-range scan of
`observations` costs ~8 s of cold reads per hour of data on the e2-micro).

**Presence classes** (of the continuous record, over the range):
`permanent` heard in ≥ 90 % of the collecting hours; `regular` 10-90 %;
`occasional` < 10 % and more than 2 hours; `transient` 1-2 hours. **Bands**
from `account_aps.band` (the advertised channel; `unknown` without one).

**Configuration changes** (`cfg`) are `ap_config_changes` rows (the one
reading of the configuration history, schema 5 rules) grouped by field:
`security` (crypt, crypt_bits, mfp_sup, mfp_req), `channel` (adv_channel,
ht_mode), `identity` (ssid, cloaked), `other` (beacon_rate, country).
**Security transitions** (`sec_transitions`) are new `ap_security_periods`:
`all`, and `level` where the security level changed (open / legacy /
psk_no_mfp / strong). An event is witnessed when its [ts, ts_end] overlaps
the window.

**Repeat audits** (`pairs.sql`): for the APs heard in both 1-h audits, the
continuous record's changes in (*t1*, *t2*] against what comparing the two
audits shows - the security configuration (crypt, crypt_bits, MFP) and the
advertised channel *at t1* vs *at t2*. A change undone before *t2* is
invisible to the pair; several changes collapse into one.

## Files

| File | Content |
|---|---|
| `summary_windows.csv` | per audit length and (metric, key): the distribution over the audits and the comparison with the continuous record (below) |
| `summary_pairs.csv` | per gap: what repeat audits detect of the changes (below) |
| `windows.csv` | one row per simulated audit, one column per `metric:key` |
| `pairs.csv` | one row per repeat-audit pair |
| `reference.csv` | the continuous totals (`metric, key, n`) |
| `sampled.csv` | every sampled audit (start, length; pair: t1, t2) |
| `timeline.csv`, `plan/`, `raw/` | the inputs and the SQL's raw output |

### `summary_windows.csv`

| Column | Meaning |
|---|---|
| `duration_s`, `metric`, `key` | audit length; `aps` (key `all`), `aps_class` (presence class), `aps_band`, `cfg` (field group), `sec_transitions` (`all`, `level`), `events` (kind), `polls` (`collecting`) |
| `windows` | simulated audits of that length |
| `continuous` | the continuous record's total over the whole range |
| `mean`, `median`, `p10`, `p90`, `min`, `max` | what one audit saw (a missing value counts 0) |
| `mean_share` | mean over the audits of seen / continuous - **the share of the continuous record one audit captures**. For `aps_class:permanent` it is the share of the permanent APs a single audit finds; for `cfg` and `events` the share of all changes / events of the range it witnesses |
| `p_any` | the share of audits that saw at least one - e.g. the **probability that a one-shot audit witnesses any security change** |
| `time_share` | audit length / the record's collecting time: the share an audit would witness if changes were spread evenly in time; compare `mean_share` with it |
| `mean_share_of_window` | `polls` only: collecting polls / (length / 30 s) - the audits' own coverage |

### `summary_pairs.csv`

| Column | Meaning |
|---|---|
| `gap_s`, `pairs` | gap between the two audits; number of pairs |
| `aps_t1_mean`, `aps_persist_share`, `aps_new_mean`, `aps_gone_mean` | APs of the first audit; the share heard again; new / gone in the second |
| `sec_aps_changed`, `ch_aps_changed` | APs (heard both times) whose security configuration / advertised channel changed in between, by the continuous record (sum over pairs) |
| `sec_aps_detected`, `ch_aps_detected` | of them, the APs whose state differs between the two audits |
| `sec_detected_share`, `ch_detected_share` | detected / changed - **what a repeat audit catches of the changes** |
| `*_transitions`, `*_transitions_per_changed_ap` | the changes behind them; how many collapse into one comparison |
| `*_pairs_any_change`, `*_pairs_any_detected` | the share of pairs with a change at all; with a detected one |

## Limits (say them in the thesis)

- The continuous record is the reference, not the ground truth: hopping
  89 channels at ~0.2 s misses frames (findings 7), so both sides miss the
  same things; the comparison is about time coverage only.
- The sensor is in a dormitory: evening and night peaks are normal, so the
  `work` variant sees fewer transient devices and fewer client-driven
  events than `24h`; report both.
- `aps` counts an AP after one frame. An audit tool that needs several
  beacons, or a person reading a list, would find fewer.
- Channel changes are dominated by the campus APs' dynamic channel
  selection (15-19 changes a day each, findings 10); security changes are
  rare (63 field changes, 57 security periods, 7 level changes up to
  2026-10-24), so their audit figures rest on few events - quote `p_any`
  with that count.
- The range contains the dataset boundaries of `wifi-sensor/docs/findings.md`
  section 7 (hop lists, collector versions) and the outages of section 6.
