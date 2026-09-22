"""Staged-attack evaluation harness for the batch detectors (read-only).

Joins an independent ground-truth log of staged attack trials (one CSV row per
trial, corroborated by a per-trial pcap taken beside the attack) against the
`detections` rows a detector wrote over the same window, and reports recall,
false positives and latency. It writes only files (a Markdown report and a CSV
of the joined rows); it never writes the database and reads it in a READ ONLY
transaction, the same discipline as detection/.

The harness is detector-agnostic: a trial and a detection are matched on the
subject BSSID and a small time window, keyed on the detector `type`. The
deauth_flood evaluation is the first user; the RSSI/evil-twin detector reuses
groundtruth.py, metrics.py and report.py unchanged and only swaps the attack
generator and the expected-outcome column.

Two entry points (python -m evaluation ...):
  report       join detections against the ground truth, emit the report + CSV
  truth-check  read each trial's pcap, fill/verify the frame count and the
               first-frame timestamp (the latency anchor); run where tshark is.

See README.md for the ground-truth schema and the run order.
"""
