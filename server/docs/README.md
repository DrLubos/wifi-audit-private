# server/docs

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
