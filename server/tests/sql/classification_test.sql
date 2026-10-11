-- Fixture test of the account-level AP reading (schema.sql, schema 9):
-- ap_classification(), ap_is_mobile() and the view account_aps.
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of the tables the
-- functions and the view read; the view is rebuilt there from its DEPLOYED
-- definition, the deployed functions resolve their tables through the search
-- path. Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/classification_test.sql
--
-- Prints NOTICE "classification: PASS". Cases: the rule order (a randomized
-- BSSID broadcasting a managed SSID is suspicious, not ignored), allowed BSSID
-- and allowed OUI with and without the vendor rule, a managed network without
-- an allowed list, friendly, a hidden AP matched by the name it was seen
-- with, ignore-randomized and weaker-than, the manual classification winning;
-- the mobile rule (randomized, vendor prefix, short lifetime only once ended);
-- the view's network key and classification source.

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  v text := rtrim(pg_get_viewdef('public.account_aps'::regclass), E'; \n');
  t text;
BEGIN
  CREATE SCHEMA classification_test;
  PERFORM set_config('search_path', 'classification_test, public', true);
  FOREACH t IN ARRAY ARRAY['accounts', 'account_settings', 'networks', 'sensors', 'aps', 'ap_sensors',
                           'ap_summary', 'sensor_summary'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING DEFAULTS INCLUDING CONSTRAINTS)', t, t);
  END LOOP;
  EXECUTE 'CREATE VIEW account_aps AS ' || v;
END $$;

INSERT INTO accounts (id, name) VALUES (1, 'test');
INSERT INTO account_settings (account_id, ignore_weaker_dbm) VALUES (1, -92);
INSERT INTO sensors (id, account_id, name, tz) VALUES (1, 1, 'test', 'UTC');
INSERT INTO sensor_summary (sensor_id, polls, polls_ok, gap_count, gap_total_s, gap_max_s, gaps,
                            devices_by_type, obs_total, obs_ap, alerts, baselines, refreshed_at,
                            last_capture_at)
VALUES (1, 0, 0, 0, 0, 0, '[]', '{}', 0, 0, 0, 0, now(), '2026-09-20 18:00+00');
-- explicit ids everywhere: the copies keep the real tables' sequence defaults
INSERT INTO networks (id, account_id, ssid, kind, expected_security, allowed_bssids, allowed_ouis) VALUES
  (1, 1, 'Corp', 'managed', 'enterprise', ARRAY['A8:00:00:00:00:01'::macaddr], ARRAY[trunc('EC:58:EA:00:00:00'::macaddr)]),
  (2, 1, 'Locked', 'managed', 'psk_or_better', '{}', '{}'),
  (3, 1, 'Neighbour', 'friendly', NULL, '{}', '{}');

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'classification: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

-- name, manual, network name, bssid, random, typical rssi -> expected
CREATE TEMP TABLE cases (name text, manual text, network text, bssid macaddr, random boolean,
                         rssi real, want text);
INSERT INTO cases VALUES
  ('allowed bssid',            NULL, 'Corp', 'A8:00:00:00:00:01', false, -60, 'managed'),
  ('allowed oui, no vendor rule', NULL, 'Corp', 'EC:58:EA:00:00:02', false, -60, 'unknown'),
  ('managed ssid, not allowed', NULL, 'Corp', 'A8:00:00:00:00:03', false, -60, 'suspicious'),
  ('randomized evil twin',     NULL, 'Corp', '9A:1F:3C:52:E0:07', true, -52, 'suspicious'),
  ('managed, no allowed list', NULL, 'Locked', 'A8:00:00:00:00:05', false, -60, 'unknown'),
  ('friendly',                 NULL, 'Neighbour', 'A8:00:00:00:00:06', false, -70, 'friendly'),
  ('randomized hotspot',       NULL, 'iPhone', '6E:2B:91:C4:0A:91', true, -70, 'ignored'),
  ('weaker than -92',          NULL, 'Far', 'A8:00:00:00:00:08', false, -95, 'ignored'),
  ('weak but no rssi',         NULL, 'Far', 'A8:00:00:00:00:09', false, NULL, 'unknown'),
  ('other',                    NULL, 'Other', 'A8:00:00:00:00:0A', false, -60, 'unknown'),
  ('hidden, no name',          NULL, NULL, 'A8:00:00:00:00:0B', false, -60, 'unknown'),
  ('manual wins',              'friendly', 'Corp', 'A8:00:00:00:00:03', false, -60, 'friendly');

SELECT pg_temp.check_rows('rules',
  'SELECT name, ap_classification(1, manual, network, bssid, random, rssi) FROM cases',
  'SELECT name, want FROM cases');

UPDATE account_settings SET managed_by_vendor = true, suspicious_unallowed = false,
                            ignore_random_bssid = false, ignore_weaker_dbm = NULL;
SELECT pg_temp.check_rows('rules, switched', 'SELECT name, ap_classification(1, manual, network, bssid, random, rssi) FROM cases', $$
  VALUES ('allowed bssid', 'managed'), ('allowed oui, no vendor rule', 'managed'),
         ('managed ssid, not allowed', 'unknown'), ('randomized evil twin', 'unknown'),
         ('managed, no allowed list', 'unknown'), ('friendly', 'friendly'),
         ('randomized hotspot', 'unknown'), ('weaker than -92', 'unknown'), ('weak but no rssi', 'unknown'),
         ('other', 'unknown'), ('hidden, no name', 'unknown'), ('manual wins', 'friendly') $$);
UPDATE account_settings SET managed_by_vendor = false, suspicious_unallowed = true,
                            ignore_random_bssid = true, ignore_weaker_dbm = -92;

-- mobile: randomized, vendor prefix (case-insensitive), lifetime < 2 h once ended
SELECT pg_temp.check_rows('mobile', $$
  SELECT x.name, ap_is_mobile(1, x.random, x.manuf, x.f, x.l, timestamptz '2026-09-20 18:00+00')
  FROM (VALUES ('random', true, NULL, timestamptz '2026-09-15 10:00+00', timestamptz '2026-09-20 17:00+00'),
               ('apple', false, 'Apple, Inc.', '2026-09-15 10:00+00', '2026-09-20 17:00+00'),
               ('samsung lower', false, 'samsung electronics', '2026-09-15 10:00+00', '2026-09-20 17:00+00'),
               ('ruckus', false, 'Ruckus Wireless', '2026-09-15 10:00+00', '2026-09-20 17:00+00'),
               ('short, ended', false, 'Ruckus Wireless', '2026-09-20 14:00+00', '2026-09-20 15:00+00'),
               ('short, still there', false, 'Ruckus Wireless', '2026-09-20 17:00+00', '2026-09-20 17:50+00'),
               ('no manuf', false, NULL, '2026-09-15 10:00+00', '2026-09-20 17:00+00')) AS x(name, random, manuf, f, l)$$, $$
  VALUES ('random', true), ('apple', true), ('samsung lower', true), ('ruckus', false),
         ('short, ended', true), ('short, still there', false), ('no manuf', false) $$);

-- the view: three APs on one sensor
INSERT INTO aps (id, account_id, bssid, first_seen, last_seen, classification) VALUES
  (1, 1, 'A8:00:00:00:00:03', '2026-09-15 10:00+00', '2026-09-20 17:00+00', NULL),
  (2, 1, 'A8:00:00:00:00:0C', '2026-09-15 10:00+00', '2026-09-20 17:00+00', NULL),
  (3, 1, 'A8:00:00:00:00:0D', '2026-09-15 10:00+00', '2026-09-20 17:00+00', 'managed');
INSERT INTO ap_sensors (ap_id, sensor_id, device_key) VALUES (1, 1, 'K3'), (2, 1, 'KC'), (3, 1, 'KD');
INSERT INTO ap_summary (sensor_id, device_key, bssid, random_bssid, manuf, ssid, hidden, first_seen, last_seen,
                        hidden_beacon, name_seen, has_baseline, trusted, n_obs, n_valid, n_floor,
                        history_rows, n_changes, adv_channel, security, rssi_typical)
VALUES (1, 'K3', 'A8:00:00:00:00:03', false, 'Ruckus Wireless', 'Corp', false, '2026-09-15 10:00+00',
        '2026-09-20 17:00+00', false, 'Corp', false, false, 10, 10, 0, 0, 0, '36', 'strong', -60),
       -- hidden, known by the name it was seen with
       (1, 'KC', 'A8:00:00:00:00:0C', false, 'Ruckus Wireless', '', true, '2026-09-15 10:00+00',
        '2026-09-20 17:00+00', true, 'Corp', false, false, 10, 10, 0, 0, 0, '6', 'strong', -60),
       (1, 'KD', 'A8:00:00:00:00:0D', false, 'Ruckus Wireless', '', true, '2026-09-15 10:00+00',
        '2026-09-20 17:00+00', true, NULL, false, false, 10, 10, 0, 0, 0, '149', 'unknown', -60);

SELECT pg_temp.check_rows('account_aps', $$
  SELECT ap_id, network_key, network_kind, band, mobile, classification, classified_by FROM account_aps$$, $$
  VALUES (1::bigint, 'Corp', 'managed', '5', false, 'suspicious', 'rule'),
         (2, 'Corp', 'managed', '2.4', false, 'suspicious', 'rule'),
         (3, 'hidden:A8:00:00:00:00:0D', NULL, '5', false, 'managed', 'manual') $$);

DO $$ BEGIN
  RAISE NOTICE 'classification: PASS (12 rule cases in two settings, 7 mobile cases, account_aps)';
END $$;

ROLLBACK;
