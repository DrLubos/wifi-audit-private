"""The sensor ingest (docs/live-upload.md): /api/ingest/v1/batch and /heartbeat.

Everything but routes.py is standard library and unit-tested on the Mac
(api/tests/test_ingest_*.py): tokens, payload validation, rate limits, the
stall events and the heartbeat helpers. The merge itself is ingest_merge() in
schema.sql, shared with the snapshot import.
"""
