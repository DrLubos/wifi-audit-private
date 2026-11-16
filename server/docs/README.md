# server/docs

- `performance.md` - the dashboard's performance on the demo box: diagnosis,
  what server schema 6 changed, before/after measurements (thesis material).
- `live-upload.md` - the live upload from the sensor (design, decisions, rollout record).
- `config-delivery.md` - delivery of the desired capture configuration to the
  sensor (schema 18): design, the staged tests, on since 2026-10-25.
- `discard-mode.md` - discard mode (schema 20): a per-sensor window whose
  uploads are accepted and not stored, excluded from loss, outages and
  coverage; how to turn it on and off, what is excluded where, the limits.
- `postgres-crashes.md` - the db container's crash-restarts: investigation,
  the cause (an orphaned health probe adopted by PID 1) and the fix of 2026-11-14.
- `disk-retention.md` - disk and raw-data retention on the box: the numbers of
  2026-11-16, the options and the decision due before ~February 2027.

Generated evaluation output and its inputs. This directory is the writable mount
of the `evaluate` compose service (`../evaluation/`).

- `evaluation.md` and `evaluation_trials.csv` are **generated** by
  `python -m evaluation report` from real detections + a ground-truth log. They
  are intentionally NOT committed until a real staged-attack run exists - the
  repo does not carry fabricated results. Re-running the tool refreshes them.
- `groundtruth.csv` / `groundtruth.enriched.csv`, the per-trial `cap/*.pcap`
  files and the intermediate `counter_only.json` are experiment artefacts; keep
  them out of git unless a specific run is being archived for the thesis.

See `../evaluation/README.md` for how to produce these, and the schema in
`../evaluation/groundtruth.example.csv`.
