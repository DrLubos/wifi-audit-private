"""Batch detectors over the server database (prototype, run on demand).

A detector reads a time window of the source tables (observations, alerts,
devices, ap_baselines - never written), turns it into per-subject episodes and
writes one row per episode into `detections` (the only table any detector
writes). Runs are idempotent: an episode already stored is refreshed, not
duplicated (see detections.py).

Nothing here is scheduled or wired to ingest; `python -m detection <detector>`
(or `docker compose run --rm detect <detector>`) runs one detector over
--from/--to and exits. The Pi collector stays dumb: all detection is here.

Detectors: deauth_flood (deauth_flood.py). See README.md.
"""
