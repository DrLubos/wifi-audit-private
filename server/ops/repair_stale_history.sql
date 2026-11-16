-- Delete configuration-history rows stored at a stale clock's time next to
-- their true-time twin (found 2026-11-16, after the snapshot import with
-- --with-client-tables).
--
-- What happened: the snapshot import loads a history row whose poll is no
-- longer in the snapshot (the sensor's retention pruned it) at its raw time,
-- since it cannot tell whether that poll's clock was right. For the boots of
-- 2026-10-19 (726f6757, 8e173531) and 2026-10-20..23 (3b93db8a) the clock was
-- wrong by 6.0 h / 8.4 h; the uploader had already delivered those rows at
-- their true times (it knows the windows: STALE_WINDOWS), so the import added
-- a second copy of each, 6-8 h too early: 3,982 rows on 2026-11-16 15:19.
--
-- The rule: a row written by that import's transaction (the xmin of its
-- sensor_uploads row, via 'snapshot', 2026-11-16 14:37:45 UTC: xid 309929)
-- whose ts lies in a boot's stale window (raw time, the same windows as
-- tests/sql/check_first_seen.sql) and that has a twin - same sensor, device
-- and every configuration field - at ts + the boot's clock error (+- 2 s).
-- Only the early copy goes. The twin rule alone is not enough: a stale
-- window overlaps the true times of its own boot, and 26 older rows have a
-- twin by coincidence (an AP flipping between two configurations) - they
-- were not written by the import and stay.
--
-- Run BEFORE ops/repair_first_seen.sql (its "first stored row" reads this
-- table) and before any refresh_rollups() from a time before 2026-10-24.
-- As the database owner, from server/, after a fresh ./backup.sh. A dry run
-- by default (the delete is made and rolled back):
--
--   docker compose exec -T db psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     < ops/repair_stale_history.sql
--
-- then, with the number of rows the dry run showed:
--
--   docker compose exec -T db psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     -v apply=1 -v expect=3982 < ops/repair_stale_history.sql
--
-- One audit_log row per sensor and boot ('history.stale_copy.delete'); the
-- sensor is marked for the refresher from the earliest twin, so the
-- configuration timelines of those APs are rebuilt within minutes.
-- Idempotent: a second run finds nothing.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL jit = off;
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v::int < 14 THEN
    RAISE EXCEPTION 'repair_stale_history: needs schema 14 (sensor_clock_boots; found %)', v;
  END IF;
END $$;

CREATE TEMP TABLE stale_copy ON COMMIT DROP AS
WITH imp AS (
  -- the import of 2026-11-16 (the newest snapshot import; refused if it is another one)
  SELECT u.sensor_id, u.xmin::text AS x, u.received_at FROM sensor_uploads u
  WHERE u.via = 'snapshot' ORDER BY u.received_at DESC LIMIT 1
), b AS (
  SELECT c.sensor_id, c.boot_id, c.epoch_true, c.epoch_true - c.epoch_stale AS delta,
         lead(c.epoch_true) OVER (PARTITION BY c.sensor_id ORDER BY c.epoch_true) AS next_true
  FROM sensor_clock_boots c
  WHERE c.epoch_stale IS NOT NULL AND c.epoch_true - c.epoch_stale >= 10
), w AS (
  SELECT b.sensor_id, b.boot_id, b.delta,
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
SELECT h.sensor_id, h.device_key, h.ts, w.boot_id, w.delta, t.ts AS twin_ts
FROM w
JOIN imp ON imp.sensor_id = w.sensor_id AND imp.received_at BETWEEN '2026-11-16 14:30+00' AND '2026-11-16 15:30+00'
JOIN device_config_history h ON h.sensor_id = w.sensor_id AND h.ts BETWEEN w.raw_from AND w.raw_to
                            AND h.xmin::text = imp.x
CROSS JOIN LATERAL (
  SELECT o.ts FROM device_config_history o
  WHERE o.sensor_id = h.sensor_id AND o.device_key = h.device_key
    AND o.ts BETWEEN h.ts + make_interval(secs => w.delta - 2) AND h.ts + make_interval(secs => w.delta + 2)
    AND o.ssid IS NOT DISTINCT FROM h.ssid AND o.cloaked IS NOT DISTINCT FROM h.cloaked
    AND o.crypt IS NOT DISTINCT FROM h.crypt AND o.crypt_bits IS NOT DISTINCT FROM h.crypt_bits
    AND o.mfp_sup IS NOT DISTINCT FROM h.mfp_sup AND o.mfp_req IS NOT DISTINCT FROM h.mfp_req
    AND o.adv_channel IS NOT DISTINCT FROM h.adv_channel AND o.ht_mode IS NOT DISTINCT FROM h.ht_mode
    AND o.beacon_rate IS NOT DISTINCT FROM h.beacon_rate AND o.country IS NOT DISTINCT FROM h.country
  ORDER BY abs(extract(epoch FROM o.ts - h.ts) - w.delta) LIMIT 1) t;

\echo == stale copies per sensor and boot
SELECT s.name AS sensor, left(c.boot_id::text, 8) AS boot, round(c.delta::numeric / 3600, 2) AS shift_h,
       count(*) AS rows, count(DISTINCT c.device_key) AS aps, min(c.ts) AS first, max(c.ts) AS last
FROM stale_copy c JOIN sensors s ON s.id = c.sensor_id
GROUP BY s.name, c.boot_id, c.delta ORDER BY s.name, min(c.ts);

SELECT count(*) AS found FROM stale_copy \gset
\if :{?expect}
\else
  \set expect -1
\endif
SELECT :found = :expect AS as_expected \gset

DELETE FROM device_config_history h
USING stale_copy c
WHERE h.sensor_id = c.sensor_id AND h.device_key = c.device_key AND h.ts = c.ts;

INSERT INTO audit_log (account_id, action, target, old, new)
SELECT s.account_id, 'history.stale_copy.delete', 'sensor:' || s.name,
       jsonb_build_object('boot', c.boot_id, 'rows', count(*), 'first', min(c.ts), 'last', max(c.ts)),
       jsonb_build_object('shift_s', round(c.delta::numeric, 2), 'kept_twins_from', min(c.twin_ts),
                          'script', 'ops/repair_stale_history.sql')
FROM stale_copy c JOIN sensors s ON s.id = c.sensor_id
GROUP BY s.account_id, s.name, c.boot_id, c.delta;

-- the refresher rebuilds the configuration timelines of the APs with history
-- from the earliest twin on (refresh_rollups(sensor, from), step 4)
INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at)
SELECT c.sensor_id, min(c.twin_ts), now(), now() FROM stale_copy c GROUP BY c.sensor_id
ON CONFLICT (sensor_id) DO UPDATE SET
  dirty_from = least(i.dirty_from, EXCLUDED.dirty_from),
  dirty_since = coalesce(i.dirty_since, EXCLUDED.dirty_since),
  dirty_at = EXCLUDED.dirty_at;

\if :{?apply}
  \if :as_expected
COMMIT;
\echo repair_stale_history: :found row(s) deleted and committed
  \else
ROLLBACK;
\echo repair_stale_history: found :found row(s), expected :expect - rolled back, nothing changed
  \endif
\else
ROLLBACK;
\echo repair_stale_history: dry run, rolled back (:found row(s)) - run again with -v apply=1 -v expect=:found
\endif
