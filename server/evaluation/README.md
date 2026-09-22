# server/evaluation - staged-attack evaluation harness

Measures a batch detector against *known* attacks: recall, false positives and
latency, from an independent ground-truth log joined against the `detections`
rows the detector wrote. It is **read-only on the database** (a `SET TRANSACTION
READ ONLY` block, same as `detection/`) and writes only files - a Markdown report
and a per-trial CSV. Nothing here attacks anything; the attack is staged
separately, by the operator, against their **own isolated test AP**.

The harness is detector-agnostic (keyed on the detector `type`): the first user
is `deauth_flood`; the RSSI/evil-twin detector will reuse `groundtruth.py`,
`metrics.py` and `report.py` unchanged and only swap the attack generator and the
expected-outcome column.

| File | Purpose |
|---|---|
| `groundtruth.py` | load the trial-log CSV; optionally fill/verify it from per-trial pcaps (tshark) |
| `metrics.py` | pure join + statistics: trial↔detection matching, Wilson-CI recall, latency, FP guard-band |
| `report.py` | render the Markdown tables, the CSV, and `evaluation.md` |
| `__main__.py` | `python -m evaluation report` / `truth-check`; the only code that reads the DB |
| `groundtruth.example.csv` | the trial-log schema, with an authorisation header row |
| `tools/attack_trial.sh`, `tools/capture_truth.sh` | operator-side, run on the attacker device (guarded) |
| `tests/` | stdlib unit tests of the pure logic (no DB, no pcap) |

Unit tests (stdlib, no database): `cd server && python3 -m unittest discover -s evaluation/tests`.

## Ground truth (independent of the sensor)

One CSV row per trial (`groundtruth.example.csv` has the columns). The wrapper on
the attacker device appends the first ten columns as it runs; `truth-check` fills
the last four from each trial's pcap. The **pcap is the source of truth** for the
frame count and the first-frame time (the latency anchor) - the attack tool's own
clock is never trusted alone. Comment/authorisation lines start with `#` and are
skipped; record who authorised the run, on whose AP and channel, in that header.

`variant` is `fast | moderate | slow | clean`; `channel_config` is
`locked | hopping`; `type` is the detector under test.

## Running it

Two steps, on two machines, because the pcap parser (tshark) and the database are
rarely on the same box:

```
# 1. attacker/analysis box (has tshark): verify claimed vs captured frames, fill first-frame times
python -m evaluation truth-check docs/groundtruth.csv -o docs/groundtruth.enriched.csv

# 2. where the DB is reachable (host, or the `evaluate` compose service):
#    a) produce the pre-v3 counterfactual WITHOUT touching the DB
docker compose run --rm detect deauth-flood --from 2026-09-24 --to 2026-09-25 \
    --counter-only --dry-run --json > docs/counter_only.json
#    b) join the real (v2) detections against the ground truth
docker compose run --rm evaluate report --groundtruth docs/groundtruth.enriched.csv \
    --sensor pi-fri --from 2026-09-24 --to 2026-09-25 \
    --counter-only-json docs/counter_only.json --out docs/evaluation.md
```

Without containers: `cd server && DATABASE_URL=... python -m evaluation report ...`
(needs psycopg 3 for the DB path; `--detections-json FILE` bypasses the DB
entirely, e.g. for a dry run against a detector `--json` dump).

The report writes `docs/evaluation.md` and `docs/evaluation_trials.csv`.

## What it computes

- **Recall** per (variant × channel_config), as `x/N` with a **Wilson 95% CI** -
  never a bare percentage, because N is small.
- **Latency**, two separate anchors: *frame-anchor* (dated evidence minus the
  pcap's first frame, ~1 s for the exact rule) and *emission-anchor* (minus the
  logged start, bounded below by the 30 s poll). They are never conflated.
- **False positives** on any AP other than the test BSSID, outside a ±300 s guard
  band around every trial; reported against the eval window and against the
  multi-week real background (the stronger denominator).
- **Channel-hop penalty**: locked recall − hopping recall per variant.
- **Old vs new**: v2 (exact rule) recall vs `--counter-only` recall; the point is
  that the `slow` variant collapses under counter-only.

## Matching (why the numbers are attributable)

A detection matches a trial when its subject BSSID equals the trial's target and
its earliest evidence timestamp is in `[start − 15 s, stop + 60 s]`. That padding
is deliberately tighter than the detector's 300 s episode gap: trials are spaced
≥ 9 min, so every detection belongs to exactly one trial. The FP guard-band
(±300 s) is separate and wider, so attack-adjacent spillover on nearby APs is
never miscounted as an organic false positive. See the plan for the full
rationale and the honesty limitations that go into the report.
