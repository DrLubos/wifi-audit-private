-- Server schema 15 -> 14: ap_config_changes back to the materialized view of
-- schemas 6-14. ONE transaction, as the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema15.sql
--
-- Then the schema-14 functions (refresh_rollups(), refresh_security_periods())
-- from the last schema-14 commit:
--   git show 1e12ca5:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
-- The view is recomputed from the same rules (no data is lost: the timeline is
-- derived). The rules view ap_config_changes_v and refresh_config_changes() go.
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '15' THEN
    RAISE EXCEPTION 'rollback_schema15: expected schema_version 15';
  END IF;
END $$;
DROP VIEW ap_channel_changes;
DROP TABLE ap_config_changes;
DROP FUNCTION refresh_config_changes(integer, text[]);
CREATE MATERIALIZED VIEW ap_config_changes AS
WITH st AS (
  SELECT sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM device_config_history
  UNION ALL
  SELECT sensor_id, device_key, NULL::timestamptz, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM devices
), known AS (
  SELECT st.sensor_id, st.device_key, st.ts, f.field, f.val
  FROM st CROSS JOIN LATERAL (VALUES
    ('ssid',        nullif(st.ssid, '')),
    ('cloaked',     CASE WHEN st.ssid = '' THEN NULL ELSE st.cloaked::text END),
    ('crypt',       st.crypt),
    ('crypt_bits',  CASE WHEN st.crypt_bits = 0 AND st.crypt IS DISTINCT FROM 'Open' THEN NULL
                         ELSE st.crypt_bits::text END),
    ('mfp_sup',     st.mfp_sup::text),
    ('mfp_req',     st.mfp_req::text),
    ('adv_channel', st.adv_channel),
    ('ht_mode',     st.ht_mode),
    ('beacon_rate', st.beacon_rate::text),
    ('country',     st.country)) AS f(field, val)
  WHERE f.val IS NOT NULL AND NOT (st.crypt IS NULL AND st.adv_channel IS NULL)
), seq AS (
  SELECT k.sensor_id, k.device_key, k.ts, k.field, k.val,
         lead(k.val) OVER (PARTITION BY k.sensor_id, k.device_key, k.field
                           ORDER BY k.ts NULLS LAST) AS new_val
  FROM known k
)
SELECT sensor_id, device_key, ts, field, val AS old_value, new_val AS new_value
FROM seq
WHERE ts IS NOT NULL AND new_val IS NOT NULL AND new_val <> val
WITH DATA;
DROP VIEW ap_config_changes_v;
CREATE UNIQUE INDEX ap_config_changes_key
  ON ap_config_changes (sensor_id, device_key, ts, field);
CREATE OR REPLACE VIEW ap_channel_changes AS
SELECT sensor_id, device_key, ts, old_value AS from_channel, new_value AS to_channel
FROM ap_config_changes
WHERE field = 'adv_channel';
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '14')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
