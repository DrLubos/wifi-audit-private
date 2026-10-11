-- The account's own data (schema 9): name, pi-fri's location and hardware, the
-- managed and friendly networks. A template: copy it OUT of the repo, replace
-- every <...> placeholder, review the networks, run it once (re-running
-- updates the same rows):
--
--   cp ops/seed_account.example.sql ~/seed_account.sql && nano ~/seed_account.sql
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ~/seed_account.sql
--
-- It refuses to run while a placeholder is left. Classification is read-time,
-- the rule events are not: the script ends with refresh_events() for every
-- sensor of the account (the "unknown BSSID with a managed SSID" events).
\set ON_ERROR_STOP on

-- >>> fill in ---------------------------------------------------------------------------
\set account_name    '<ACCOUNT NAME>'
\set pi_fri_location '<PI-FRI LOCATION>'
-- <<< -----------------------------------------------------------------------------------

BEGIN;
CREATE TEMP TABLE seed_values ON COMMIT DROP AS
SELECT :'account_name'::text AS account_name, :'pi_fri_location'::text AS location;
DO $$
BEGIN
  IF (SELECT account_name LIKE '<%' OR location LIKE '<%' FROM seed_values) THEN
    RAISE EXCEPTION 'seed_account: replace the <...> placeholders first';
  END IF;
END $$;

UPDATE accounts SET name = (SELECT account_name FROM seed_values), tz = 'Europe/Bratislava' WHERE id = 1;
UPDATE sensors SET location = (SELECT location FROM seed_values),
                   hardware = 'Raspberry Pi 3B+ · RTL8821CU (USB)'
WHERE name = 'pi-fri';

-- Managed and friendly networks. The rows below are what the data shows on
-- 2026-10-11 (campus SSIDs, the OUIs of their BSSIDs) - check and edit them.
-- allowed_ouis: the first three octets with zeros (trunc()); allowed_bssids:
-- full BSSIDs. require_mfp: true makes every BSSID without "MFP required"
-- non-compliant (IK-WIFI-DOT1X advertises no MFP at all today).
INSERT INTO networks (account_id, ssid, kind, expected_security, require_mfp, allowed_bssids, allowed_ouis, note)
VALUES
  (1, 'IK-WIFI-DOT1X', 'managed', 'enterprise', false, '{}',
   ARRAY['EC:58:EA:00:00:00', '3C:46:A1:00:00:00', 'F0:6F:CE:00:00:00']::macaddr[], 'campus 802.1X (Ruckus)'),
  (1, 'IK-WIFI', 'managed', 'open_allowed', false, '{}',
   ARRAY['EC:58:EA:00:00:00', '3C:46:A1:00:00:00']::macaddr[], 'campus guest network (Ruckus)'),
  (1, 'FRI_wifi', 'managed', 'psk_or_better', false, '{}',
   ARRAY['78:9A:18:00:00:00']::macaddr[], 'MikroTik')
ON CONFLICT (account_id, ssid) DO UPDATE SET
  kind = EXCLUDED.kind, expected_security = EXCLUDED.expected_security, require_mfp = EXCLUDED.require_mfp,
  allowed_bssids = EXCLUDED.allowed_bssids, allowed_ouis = EXCLUDED.allowed_ouis, note = EXCLUDED.note,
  updated_at = now();

-- With only OUIs allowed, a BSSID of an allowed vendor is "managed" only when
-- this rule is on (otherwise it stays "unknown" until it is listed or
-- classified by hand); a BSSID of another vendor with a managed SSID is
-- "suspicious" either way.
UPDATE account_settings SET managed_by_vendor = true, updated_at = now() WHERE account_id = 1;
UPDATE accounts SET write_version = write_version + 1 WHERE id = 1;

SELECT id, name, tz FROM accounts;
SELECT name, location, hardware FROM sensors;
SELECT ssid, kind, expected_security, require_mfp, allowed_ouis FROM networks ORDER BY ssid;
COMMIT;

SELECT s.name, e.* FROM sensors s CROSS JOIN LATERAL refresh_events(s.id) e WHERE s.account_id = 1;
SELECT classification, count(*) FROM account_aps GROUP BY 1 ORDER BY 2 DESC;
