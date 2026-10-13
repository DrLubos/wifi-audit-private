-- Server schema 13 -> 12: the catalog and the attachment history again. ONE
-- transaction, as the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema13.sql
--
-- The adapter of each sensor goes back into the catalog and is attached since
-- its first poll (the schema-11 seed); every 'hardware_change' entry becomes
-- a sensor_hardware row (no antenna: the antennas were never recorded in
-- schema 13) and leaves the history. Then apply the schema.sql of the last
-- schema-12 commit (git show fa66258:server/schema.sql | psql ...) and
-- recompute the baselines (SELECT s.id, refresh_ap_baselines(s.id, 200, 2) FROM sensors s).
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '13' THEN
    RAISE EXCEPTION 'rollback_schema13: expected schema_version 13';
  END IF;
END $$;

CREATE TABLE capture_hardware (
  id         serial PRIMARY KEY,
  account_id integer NOT NULL DEFAULT 1 REFERENCES accounts(id),
  kind       text NOT NULL CHECK (kind IN ('adapter', 'antenna')),
  name       text NOT NULL CHECK (name <> ''),
  gain_dbi   real CONSTRAINT capture_hardware_gain_check CHECK (gain_dbi BETWEEN -10 AND 30),
  bands      text[] NOT NULL DEFAULT '{}' CHECK (bands <@ ARRAY['2.4', '5', '6']),
  connector  text,
  note       text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, kind, name));
CREATE TABLE sensor_hardware (
  sensor_id  integer NOT NULL REFERENCES sensors(id),
  valid_from timestamptz NOT NULL,
  adapter_id integer NOT NULL REFERENCES capture_hardware(id),
  antenna_id integer REFERENCES capture_hardware(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL DEFAULT 'operator',
  note       text,
  PRIMARY KEY (sensor_id, valid_from));

INSERT INTO capture_hardware (account_id, kind, name, bands)
SELECT DISTINCT s.account_id, 'adapter', a.name, a.bands
FROM sensor_adapters a JOIN sensors s ON s.id = a.sensor_id
ON CONFLICT DO NOTHING;
INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, created_by, note)
SELECT a.sensor_id, coalesce((SELECT min(p.ts) FROM polls p WHERE p.sensor_id = a.sensor_id), a.updated_at),
       (SELECT h.id FROM capture_hardware h WHERE h.account_id = s.account_id AND h.kind = 'adapter' AND h.name = a.name),
       'seed', 'restored by the schema-13 rollback'
FROM sensor_adapters a JOIN sensors s ON s.id = a.sensor_id;
INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, created_by, note)
SELECT n.sensor_id, n.starts_at, (SELECT h.adapter_id FROM sensor_hardware h WHERE h.sensor_id = n.sensor_id LIMIT 1),
       'operator', n.label
FROM sensor_annotations n
WHERE n.kind = 'hardware_change' AND EXISTS (SELECT 1 FROM sensor_hardware h WHERE h.sensor_id = n.sensor_id)
ON CONFLICT DO NOTHING;
DELETE FROM sensor_annotations WHERE kind = 'hardware_change';
ALTER TABLE sensor_annotations DROP CONSTRAINT sensor_annotations_kind_check;
ALTER TABLE sensor_annotations ADD CONSTRAINT sensor_annotations_kind_check
  CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change'));
DROP TABLE sensor_adapters;
UPDATE schema_meta SET value = '12' WHERE key = 'schema_version';
COMMIT;
