-- Fixture test of the materialized view ap_config_changes (schema.sql, schema 5,
-- materialized in schema 6) and of ap_channel_changes, which is derived from it.
--
-- Writes nothing: everything runs in one transaction that is rolled back. The
-- DEPLOYED definitions are read with pg_get_viewdef() first; then a scratch
-- schema (a materialized view cannot be TEMP) gets empty copies of devices and
-- device_config_history, the view is built there from those definitions, the
-- fixtures go in and the view is refreshed. No real row is read or written.
-- Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/ap_config_changes_test.sql
--
-- Prints NOTICE "ap_config_changes: PASS"; a failing case raises an exception
-- that lists the missing and the unexpected rows.
--
-- The point of the cases (developer's requirement, 2026-09-28): the rules that
-- hide the pre-v5 churn - NULL = unknown, crypt_bits 0 = unknown unless the
-- crypt string is 'Open', the hidden-beacon/named-record alternation - must
-- never hide a real security change such as a WPA2 -> Open downgrade.

\set ON_ERROR_STOP on
BEGIN;

-- 1. definitions first (while the names resolve to public, so pg_get_viewdef()
--    leaves them unqualified), then the scratch schema, first on the search path
DO $$
DECLARE
  cfg text := rtrim(pg_get_viewdef('public.ap_config_changes'::regclass), E'; \n');
  chan text := rtrim(pg_get_viewdef('public.ap_channel_changes'::regclass), E'; \n');
BEGIN
  IF (SELECT relkind FROM pg_class WHERE oid = 'public.ap_config_changes'::regclass) <> 'm' THEN
    RAISE EXCEPTION 'ap_config_changes is not a materialized view: apply schema.sql (schema 6) first';
  END IF;
  CREATE SCHEMA ap_config_changes_test;
  PERFORM set_config('search_path', 'ap_config_changes_test, public', true);
  CREATE TABLE devices (LIKE public.devices INCLUDING DEFAULTS);
  CREATE TABLE device_config_history (LIKE public.device_config_history INCLUDING DEFAULTS);
  EXECUTE 'CREATE MATERIALIZED VIEW ap_config_changes AS ' || cfg || ' WITH NO DATA';
  EXECUTE 'CREATE VIEW ap_channel_changes AS ' || chan;
END $$;

-- 2. fixtures. W = a WPA2 crypt string, X = its bitfield. History rows hold
--    the REPLACED configuration at ts; devices the current one.
--    Column order: key, ts, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
--                  adv_channel, ht_mode, beacon_rate, country
INSERT INTO device_config_history (sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits,
                                   mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
SELECT 1, k, timestamptz '2026-09-20 00:00+00' + t * interval '1 min', s, c, cr, cb, ms, mr, ch, ht, br, co
FROM (VALUES
  -- T1 direct downgrade WPA2 -> Open (bits X -> 0, MFP capable -> not)
  ('T1', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  -- T2 the same downgrade behind pre-v5 churn: full, incomplete, full, incomplete, then Open
  ('T2', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T2', 2, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, NULL, NULL, NULL, NULL),
  ('T2', 3, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T2', 4, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, NULL, NULL, NULL, NULL),
  -- T3 downgrade first seen in an incomplete record that already says Open (bits 0)
  ('T3', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T3', 2, 'Net', false, 'Open',                   0,            false, false, NULL, NULL, NULL, NULL),
  -- T4 crypt_bits value <-> 0 under an unchanged WPA2 string (Kismet's missing-field 0)
  ('T4', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T4', 2, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 0,            true,  false, '6', 'HT20', 1024, 'SK'),
  ('T4', 3, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T4', 4, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 0,            true,  false, '6', 'HT20', 1024, 'SK'),
  -- T5 pre-v5 churn only: A -> incomplete -> A -> incomplete -> A
  ('T5', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T5', 2, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, NULL, NULL, NULL, NULL),
  ('T5', 3, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6', 'HT20', 1024, 'SK'),
  ('T5', 4, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, NULL, NULL, NULL, NULL),
  -- T6 hidden AP: cloaked beacon ('' , cloaked) <-> named, uncloaked record
  ('T6', 1, '',    true,  'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '1', 'HT20', 1024, 'SK'),
  ('T6', 2, 'Hid', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '1', 'HT20', 1024, 'SK'),
  ('T6', 3, '',    true,  'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '1', 'HT20', 1024, 'SK'),
  -- T7 a real channel change 1 -> 11
  ('T7', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '1', 'HT20', 1024, 'SK'),
  -- T8 first beacon record after a no-record state (flags 0/false, bits 0, crypt and channel NULL)
  ('T8', 1, NULL,  false, NULL,                     0,            false, false, NULL, NULL, NULL, NULL),
  -- T9 upgrade Open -> WPA2 (Open's bitfield 0 is a real value)
  ('T9', 1, 'Net', false, 'Open',                   0,            false, false, '6', 'HT20', 1024, 'SK'),
  -- T10 foreign country and beacon rate change are listed
  ('T10', 1, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true, false, '6', 'HT20', 1024, 'SK')
) AS f(k, t, s, c, cr, cb, ms, mr, ch, ht, br, co);

INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, ssid, cloaked, crypt,
                     crypt_bits, mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
SELECT 1, k, macaddr '02:00:00:00:00:00', 'ap', timestamptz '2026-09-19 00:00+00',
       timestamptz '2026-09-21 00:00+00', s, c, cr, cb, ms, mr, ch, ht, br, co
FROM (VALUES
  ('T1', 'Net', false, 'Open',                   0,            false, false, '6',  'HT20', 1024, 'SK'),
  ('T2', 'Net', false, 'Open',                   0,            false, false, '6',  'HT20', 1024, 'SK'),
  ('T3', 'Net', false, 'Open',                   0,            false, false, '6',  'HT20', 1024, 'SK'),
  ('T4', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6',  'HT20', 1024, 'SK'),
  ('T5', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6',  'HT20', 1024, 'SK'),
  ('T6', 'Hid', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '1',  'HT20', 1024, 'SK'),
  ('T7', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '11', 'HT20', 1024, 'SK'),
  ('T8', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6',  'HT20', 1024, 'SK'),
  ('T9', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true,  false, '6',  'HT20', 1024, 'SK'),
  ('T10', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true, false, '6',  'HT20', 100,  'US')
) AS f(k, s, c, cr, cb, ms, mr, ch, ht, br, co);

REFRESH MATERIALIZED VIEW ap_config_changes;

-- 3. expected rows: (key, minute of the replaced row, field, old, new)
CREATE TEMP TABLE expected (device_key text, t int, field text, old_value text, new_value text);
INSERT INTO expected VALUES
  ('T1', 1, 'crypt', 'WPA2 WPA2-PSK AES-CCMP', 'Open'),
  ('T1', 1, 'crypt_bits', '274945016842', '0'),
  ('T1', 1, 'mfp_sup', 'true', 'false'),
  ('T2', 4, 'crypt', 'WPA2 WPA2-PSK AES-CCMP', 'Open'),
  ('T2', 4, 'crypt_bits', '274945016842', '0'),
  ('T2', 4, 'mfp_sup', 'true', 'false'),
  ('T3', 1, 'crypt', 'WPA2 WPA2-PSK AES-CCMP', 'Open'),
  ('T3', 1, 'crypt_bits', '274945016842', '0'),
  ('T3', 1, 'mfp_sup', 'true', 'false'),
  ('T7', 1, 'adv_channel', '1', '11'),
  ('T9', 1, 'crypt', 'Open', 'WPA2 WPA2-PSK AES-CCMP'),
  ('T9', 1, 'crypt_bits', '0', '274945016842'),
  ('T9', 1, 'mfp_sup', 'false', 'true'),
  ('T10', 1, 'beacon_rate', '1024', '100'),
  ('T10', 1, 'country', 'SK', 'US');

-- 4. compare both ways
DO $$
DECLARE
  missing text;
  unexpected text;
  chan text;
BEGIN
  SELECT string_agg(format('%s@%s %s: %s -> %s', device_key, t, field, old_value, new_value), '; ')
    INTO missing
  FROM (SELECT device_key, t, field, old_value, new_value FROM expected
        EXCEPT
        SELECT device_key, extract(minute FROM ts)::int, field, old_value, new_value FROM ap_config_changes) m;
  SELECT string_agg(format('%s@%s %s: %s -> %s', device_key, t, field, old_value, new_value), '; ')
    INTO unexpected
  FROM (SELECT device_key, extract(minute FROM ts)::int AS t, field, old_value, new_value FROM ap_config_changes
        EXCEPT
        SELECT device_key, t, field, old_value, new_value FROM expected) u;
  IF missing IS NOT NULL OR unexpected IS NOT NULL THEN
    RAISE EXCEPTION 'ap_config_changes: FAIL - missing: %; unexpected: %',
      coalesce(missing, 'none'), coalesce(unexpected, 'none');
  END IF;
  SELECT string_agg(format('%s %s -> %s', device_key, from_channel, to_channel), '; ') INTO chan
  FROM ap_channel_changes;
  IF chan IS DISTINCT FROM 'T7 1 -> 11' THEN
    RAISE EXCEPTION 'ap_channel_changes: FAIL - got %, expected T7 1 -> 11', coalesce(chan, 'no rows');
  END IF;
  RAISE NOTICE 'ap_config_changes: PASS (15 expected changes over 10 fixture APs; churn, crypt_bits 0 and hidden-beacon cases silent)';
END $$;

ROLLBACK;
