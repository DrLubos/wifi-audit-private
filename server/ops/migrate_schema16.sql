-- Server schema 15 -> 16 (schema.sql, schema 16): refresh_runs, one row per
-- refresher step (the refresh-duration series of the system evaluation,
-- evaluation/pipeline/; the container log is lost on every recreate). ONE
-- transaction: any error leaves schema 15 as it is. Run through
-- ops/migrate_schema16.sh. Nothing existing changes; the refresher writes the
-- table once its new code is deployed (it skips writing while it is missing).
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '15' THEN
    RAISE EXCEPTION 'migrate_schema16: expected schema_version 15';
  END IF;
END $$;

\echo [1/2] refresh_runs
CREATE TABLE refresh_runs (
  id           bigserial PRIMARY KEY,
  started_at   timestamptz NOT NULL,
  step         text NOT NULL CHECK (step IN ('rollups', 'contact_periods', 'ap_baselines', 'round')),
  sensor_id    integer REFERENCES sensors(id),
  duration_s   real NOT NULL,
  ok           boolean NOT NULL DEFAULT true,
  refresh_from timestamptz,                    -- rollups: the dirty mark refreshed from
  waited_s     real,                           -- rollups: how long the oldest new data waited
  ap_hours     integer,                        -- rollups
  sensor_hours integer,                        -- rollups
  gaps         integer,                        -- contact_periods: unreachable periods written
  baselines    integer,                        -- ap_baselines
  next_in_s    real,                           -- round: delay to the next (duty-cycle cap when > the interval)
  error        text);
CREATE INDEX refresh_runs_started ON refresh_runs (started_at);

\echo [2/2] schema_meta 16
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '16')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
