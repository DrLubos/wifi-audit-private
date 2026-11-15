-- Read-only check: devices.first_seen against the stale-clock stretches of
-- the sensors' boots (docs/live-upload.md 8.3). Runs as claude_ro:
--
--   docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB"' \
--     < tests/sql/check_first_seen.sql
--
-- A sensor without NTP runs on a stale clock; the device row on the sensor
-- keeps a first_seen written then for good, and the uploader shifts it only
-- while it knows that boot's stale window. Sent as it is, the merge's
-- LEAST() moves the stored first_seen back by the clock's error (6 APs until
-- 2026-11-15: three by 8 h 25 min from the boot of 2026-10-20..23, three by
-- 2.8 - 5.5 h from the boots of 2026-10-19).
--
-- 1. The stale window of every corrected boot, from sensor_clock_boots and
--    the offsets of its polls: [first, last poll on the stale clock] +- 120 s
--    and the shift. `stale_windows_entry` is the boot's entry for
--    STALE_WINDOWS in the sensor's uploader.conf - needed for a boot whose
--    polls the sensor's retention removed and the uploader's state does not
--    remember (the state was lost, or the boot is older than uploader 1.3).
-- 2. Expected: no row. An AP whose first_seen lies in a stale window and is
--    more than 5 minutes before its first stored row was not seen at that
--    time (a seen AP has an observation within one poll): the value is a
--    stale clock's. `repaired` = the earlier of its first stored row and the
--    shifted value - what ops/repair_first_seen.sql writes (the same rule;
--    keep the two equal). `seen_at_shifted` = a row within 2 minutes after
--    the shifted value (the sighting the stale value was written at).
--    Not told apart by the rule: a device truly first seen at such a time
--    that was no AP then and has no row (looks the same; none so far).
\set ON_ERROR_STOP on
\pset pager off

\echo == 1. stale windows per corrected boot
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
SELECT s.name AS sensor, left(w.boot_id::text, 8) AS boot, w.source, w.polls, w.raw_from, w.raw_to,
       round(w.delta::numeric, 2) AS shift_s,
       w.boot_id || ':' || extract(epoch FROM w.raw_from)::bigint || ':' || extract(epoch FROM w.raw_to)::bigint
         || ':' || round(w.delta::numeric, 2) AS stale_windows_entry
FROM w JOIN sensors s ON s.id = w.sensor_id
ORDER BY s.name, w.raw_from;

\echo == 2. APs with a stale first_seen (expected: none)
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
), cand AS (
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
  ORDER BY d.sensor_id, d.device_key, least(e.first_row, d.first_seen + make_interval(secs => round(w.delta)))
)
SELECT s.name AS sensor, c.mac AS bssid, left(c.boot_id::text, 8) AS boot, c.first_seen, c.first_row,
       c.shifted, c.repaired, c.repaired - c.first_seen AS moved_by,
       EXISTS (SELECT 1 FROM observations o
               WHERE o.sensor_id = c.sensor_id AND o.device_key = c.device_key
                 AND o.ts >= c.shifted AND o.ts < c.shifted + interval '2 minutes') AS seen_at_shifted
FROM cand c JOIN sensors s ON s.id = c.sensor_id
ORDER BY s.name, c.first_seen;
