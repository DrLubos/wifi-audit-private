-- Server schema 7 -> 8: ap_load_hourly (AP load per hour and advertised
-- channel), ap_channel_at(), load_ap_load_samples(). Additive: no existing
-- table is touched or locked. ONE transaction: any error leaves schema 7
-- exactly as it was. Run through ops/migrate_schema8.sh (afterwards it applies
-- schema.sql and runs the SQL fixture tests).
--
-- What happens:
--   1. checks: schema 7, no ap_load_hourly yet
--   2. the table ap_load_hourly + its hour index
--   3. the functions ap_channel_at() and load_ap_load_samples()
--   4. schema_meta 8
--
-- The table starts empty. It is filled by the importer (collector buffer v7
-- rows, and the per-poll load columns of buffer <= v5 rows in a snapshot) and
-- by ops/backfill_ap_load_from_dump.sh (the schema-6 observations of the
-- pre-migration dump).
\set ON_ERROR_STOP on
\pset pager off
\timing on
SET lock_timeout = '10s';

BEGIN;

\echo [1/4] checks
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '7' THEN
    RAISE EXCEPTION 'migrate_schema8: expected schema_version 7, found %', v;
  END IF;
  IF to_regclass('ap_load_hourly') IS NOT NULL THEN
    RAISE EXCEPTION 'migrate_schema8: ap_load_hourly already exists';
  END IF;
END $$;

\echo [2/4] ap_load_hourly
-- Same definition as in schema.sql (tests/test_schema_files.py keeps them equal).
CREATE TABLE ap_load_hourly (
  sensor_id     integer NOT NULL,
  device_key    text NOT NULL,
  hour          timestamptz NOT NULL,        -- start of the UTC hour
  channel       text NOT NULL,               -- advertised channel at those polls ('' = unknown)
  polls         integer NOT NULL,            -- polls with a new observation of the AP on this channel
  clients_polls integer NOT NULL,            -- polls with a Kismet client count
  clients_max   integer,                     -- Kismet num_associated_clients
  clients_sum   integer,
  qbss_polls    integer NOT NULL,            -- polls whose beacon carried a BSS Load element
  qbss_max      integer,                     -- AP-reported station count
  qbss_sum      integer,
  util_max      real,                        -- AP-reported channel utilisation, %
  util_sum      real,
  source        text NOT NULL CHECK (source IN ('buffer', 'v5_obs')),
  PRIMARY KEY (sensor_id, device_key, hour, channel),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);
COMMENT ON TABLE ap_load_hourly IS
  'AP load per UTC hour and advertised channel: client count, BSS Load station count and channel utilisation as sums and counts (schema 8). Source data, not derived; the row with more polls wins on every load.';

\echo [3/4] ap_channel_at(), load_ap_load_samples()
-- Same definitions as in schema.sql (tests/test_schema_files.py keeps them equal).
CREATE OR REPLACE FUNCTION ap_channel_at(p_sensor_id integer, p_device_key text, p_ts timestamptz)
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT coalesce(
    (SELECT h.adv_channel FROM device_config_history h
     WHERE h.sensor_id = p_sensor_id AND h.device_key = p_device_key AND h.ts > p_ts
       AND h.adv_channel IS NOT NULL
     ORDER BY h.ts LIMIT 1),
    (SELECT d.adv_channel FROM devices d
     WHERE d.sensor_id = p_sensor_id AND d.device_key = p_device_key))
$$;

CREATE OR REPLACE FUNCTION load_ap_load_samples(p_sensor_id integer)
RETURNS TABLE (hours integer, inserted integer, updated integer)
LANGUAGE plpgsql
SET work_mem = '64MB'      -- the backfill sorts ~2.5M samples
SET jit = off
AS $$
#variable_conflict use_column
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE id = p_sensor_id) THEN
    RAISE EXCEPTION 'load_ap_load_samples: unknown sensor_id %', p_sensor_id;
  END IF;
  RETURN QUERY
  WITH s AS (
    SELECT DISTINCT ON (x.device_key, x.ts)
           x.device_key, x.ts, x.n_clients, x.qbss_stations, x.util_pct
    FROM stage_load_samples x
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = x.device_key AND d.type = 'ap'
    ORDER BY x.device_key, x.ts
  ), u AS (      -- the samples and the APs' known channels from the history, one stream
    SELECT s.device_key, s.ts, 0 AS kind, NULL::text AS h_channel,
           s.n_clients, s.qbss_stations, s.util_pct
    FROM s
    UNION ALL
    SELECT h.device_key, h.ts, 1, h.adv_channel, NULL, NULL, NULL
    FROM device_config_history h
    WHERE h.sensor_id = p_sensor_id AND h.adv_channel IS NOT NULL
      AND h.device_key IN (SELECT s.device_key FROM s)
  ), g AS (      -- grp = known history channels at or after the row; a history row sorts
                 -- before a sample of the same ts (its configuration ended there)
    SELECT u.*, count(u.h_channel) OVER (PARTITION BY u.device_key ORDER BY u.ts, u.kind DESC
                                         ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS grp
    FROM u
  ), n AS (      -- the channel of the next history row, as ap_channel_at() reads it (set-based:
                 -- one lookup per sample costs ~0.3 ms on the e2-micro, measured 2026-10-11)
    SELECT g.*, max(g.h_channel) OVER (PARTITION BY g.device_key, g.grp) AS next_channel
    FROM g
  ), c AS (
    SELECT n.device_key, date_trunc('hour', n.ts, 'UTC') AS hour,
           coalesce(n.next_channel, d.adv_channel, '') AS channel,
           n.n_clients,
           coalesce(n.qbss_stations > 0 OR n.util_pct > 0, false) AS qbss,
           n.qbss_stations, n.util_pct
    FROM n
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = n.device_key
    WHERE n.kind = 0
  ), a AS (
    SELECT c.device_key, c.hour, c.channel,
           count(*)::integer AS polls,
           count(c.n_clients)::integer AS clients_polls,
           max(c.n_clients) AS clients_max,
           sum(c.n_clients)::integer AS clients_sum,
           (count(*) FILTER (WHERE c.qbss))::integer AS qbss_polls,
           max(c.qbss_stations) FILTER (WHERE c.qbss) AS qbss_max,
           (sum(c.qbss_stations) FILTER (WHERE c.qbss))::integer AS qbss_sum,
           max(c.util_pct) FILTER (WHERE c.qbss) AS util_max,
           sum(c.util_pct) FILTER (WHERE c.qbss) AS util_sum
    FROM c
    GROUP BY 1, 2, 3
  ), up AS (
    INSERT INTO ap_load_hourly AS t (sensor_id, device_key, hour, channel, polls, clients_polls,
                                     clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum,
                                     util_max, util_sum, source)
    SELECT p_sensor_id, a.device_key, a.hour, a.channel, a.polls, a.clients_polls,
           a.clients_max, a.clients_sum, a.qbss_polls, a.qbss_max, a.qbss_sum,
           a.util_max, a.util_sum, 'v5_obs'
    FROM a
    ON CONFLICT (sensor_id, device_key, hour, channel) DO UPDATE SET
        polls = EXCLUDED.polls, clients_polls = EXCLUDED.clients_polls,
        clients_max = EXCLUDED.clients_max, clients_sum = EXCLUDED.clients_sum,
        qbss_polls = EXCLUDED.qbss_polls, qbss_max = EXCLUDED.qbss_max,
        qbss_sum = EXCLUDED.qbss_sum, util_max = EXCLUDED.util_max,
        util_sum = EXCLUDED.util_sum, source = EXCLUDED.source
      WHERE EXCLUDED.polls > t.polls
    RETURNING (t.xmax = 0) AS is_insert
  )
  SELECT (SELECT count(*) FROM a)::integer,
         (count(*) FILTER (WHERE up.is_insert))::integer,
         (count(*) FILTER (WHERE NOT up.is_insert))::integer
  FROM up;
END
$$;

\echo [4/4] schema 8
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '8')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
SELECT clock_timestamp()::timestamptz(0) AS committed;
