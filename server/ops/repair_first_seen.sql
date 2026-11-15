-- Repair devices.first_seen of APs that carry a stale clock's value
-- (docs/live-upload.md 8.3; the rule and what it cannot tell apart:
-- tests/sql/check_first_seen.sql, which lists the same rows read-only - run
-- it first, and keep the two rules equal).
--
-- The sensor keeps a first_seen written on a stale clock for good; an
-- uploader that no longer knew that boot's stale window sent it as it was and
-- the merge's LEAST() kept it: the AP looks first seen hours before its
-- first row. The repaired value is the earlier of the AP's first stored row
-- (observation or configuration history) and the stale value shifted by the
-- boot's clock error - both are sightings, so the true value is not later.
--
-- BEFORE: the sensor's uploader must know the boots' stale windows (uploader
-- >= 1.4 with STALE_WINDOWS in uploader.conf, entries printed by the check),
-- or its next batch with one of these APs moves the value back again.
--
-- Run as the database owner, from server/, after a fresh ./backup.sh. A dry
-- run by default (the update is made and rolled back):
--
--   docker compose exec -T db psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     < ops/repair_first_seen.sql
--
-- then, if the list is right, with the number of rows it showed:
--
--   docker compose exec -T db psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     -v apply=1 -v expect=6 < ops/repair_first_seen.sql
--
-- One audit_log row per AP ('device.first_seen.repair', old and new value);
-- the sensor is marked for the refresher (ap_summary is rebuilt within a
-- minute). Idempotent: a second run finds nothing.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v::int < 14 THEN
    RAISE EXCEPTION 'repair_first_seen: needs schema 14 (sensor_clock_boots; found %)', v;
  END IF;
END $$;

CREATE TEMP TABLE repair_first_seen ON COMMIT DROP AS
WITH b AS (
  SELECT c.sensor_id, c.boot_id, c.source, c.epoch_true, c.epoch_true - c.epoch_stale AS delta,
         lead(c.epoch_true) OVER (PARTITION BY c.sensor_id ORDER BY c.epoch_true) AS next_true
  FROM sensor_clock_boots c
  WHERE c.epoch_stale IS NOT NULL AND c.epoch_true - c.epoch_stale >= 10
), w AS (
  SELECT b.sensor_id, b.boot_id, b.source, b.delta, r.polls,
         r.raw_first - interval '120 seconds' AS raw_from, r.raw_last + interval '120 seconds' AS raw_to
  FROM b CROSS JOIN LATERAL (
    SELECT count(*) AS polls, min(p.ts - make_interval(secs => p.ts_offset_s)) AS raw_first,
           max(p.ts - make_interval(secs => p.ts_offset_s)) AS raw_last
    FROM polls p
    WHERE p.sensor_id = b.sensor_id AND p.ts_offset_s IS NOT NULL
      AND p.ts >= to_timestamp(b.epoch_true) - interval '1 second'
      AND (b.next_true IS NULL OR p.ts < to_timestamp(b.next_true) - interval '1 second')) r
  WHERE r.polls > 0
)
SELECT DISTINCT ON (d.sensor_id, d.device_key)
       d.sensor_id, d.device_key, d.mac, d.first_seen, w.boot_id, w.delta, e.first_row,
       d.first_seen + make_interval(secs => round(w.delta)) AS shifted,
       least(e.first_row, d.first_seen + make_interval(secs => round(w.delta))) AS repaired
FROM w
JOIN devices d ON d.sensor_id = w.sensor_id AND d.ever_ap AND d.first_seen BETWEEN w.raw_from AND w.raw_to
CROSS JOIN LATERAL (
  SELECT least((SELECT min(o.ts) FROM observations o
                WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
               (SELECT min(h.ts) FROM device_config_history h
                WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key)) AS first_row) e
WHERE e.first_row - d.first_seen > interval '5 minutes'
ORDER BY d.sensor_id, d.device_key, least(e.first_row, d.first_seen + make_interval(secs => round(w.delta)));

\echo == APs to repair
SELECT s.name AS sensor, r.mac AS bssid, left(r.boot_id::text, 8) AS boot, r.first_seen, r.first_row, r.shifted,
       r.repaired, r.repaired - r.first_seen AS moved_by
FROM repair_first_seen r JOIN sensors s ON s.id = r.sensor_id
ORDER BY s.name, r.first_seen;

SELECT count(*) AS found FROM repair_first_seen \gset
\if :{?expect}
\else
  \set expect -1
\endif
SELECT :found = :expect AS as_expected \gset

UPDATE devices d SET first_seen = r.repaired
FROM repair_first_seen r
WHERE d.sensor_id = r.sensor_id AND d.device_key = r.device_key AND d.first_seen = r.first_seen
  AND r.repaired > d.first_seen;

INSERT INTO audit_log (account_id, action, target, old, new)
SELECT s.account_id, 'device.first_seen.repair', 'ap:' || r.mac,
       jsonb_build_object('sensor', s.name, 'first_seen', r.first_seen),
       jsonb_build_object('sensor', s.name, 'first_seen', r.repaired, 'boot', r.boot_id,
                          'shift_s', round(r.delta::numeric, 2), 'first_row', r.first_row)
FROM repair_first_seen r JOIN sensors s ON s.id = r.sensor_id;

-- the refresher's work: ap_summary carries first_seen (a device-only change:
-- the current hour and the whole-sensor steps, as in ingest_merge())
INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at)
SELECT DISTINCT r.sensor_id, now(), now(), now() FROM repair_first_seen r
ON CONFLICT (sensor_id) DO UPDATE SET
  dirty_from = least(i.dirty_from, EXCLUDED.dirty_from),
  dirty_since = coalesce(i.dirty_since, EXCLUDED.dirty_since),
  dirty_at = EXCLUDED.dirty_at;

\echo == after the update
SELECT s.name AS sensor, r.mac AS bssid, d.first_seen, r.first_row, r.first_row - d.first_seen AS before_first_row
FROM repair_first_seen r JOIN sensors s ON s.id = r.sensor_id
JOIN devices d ON d.sensor_id = r.sensor_id AND d.device_key = r.device_key
ORDER BY s.name, d.first_seen;

\if :{?apply}
  \if :as_expected
COMMIT;
\echo repair_first_seen: :found row(s) repaired and committed
  \else
ROLLBACK;
\echo repair_first_seen: found :found row(s), expected :expect - rolled back, nothing changed
  \endif
\else
ROLLBACK;
\echo repair_first_seen: dry run, rolled back (:found row(s)) - run again with -v apply=1 -v expect=:found
\endif
