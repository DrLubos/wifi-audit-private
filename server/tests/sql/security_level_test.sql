-- Fixture test of the security functions (schema.sql, schema 9): crypt_kind(),
-- security_level(), security_label(), security_meets(), security_detail(),
-- mfp_state(), security_rank(), kismet_severity(). Pure functions: nothing is
-- read or written; the transaction is rolled back anyway. Runs as any role:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/security_level_test.sql
--
-- Prints NOTICE "security_level: PASS". The cases: every crypt / crypt_bits /
-- MFP combination seen on the live APs on 2026-10-11 (PLAN.md section 5.3),
-- plus synthetic bitfields built from Kismet 2025-09-R1's dot11_crypt_* bit
-- positions (packet_ieee80211.h) for the cases not seen live.

\set ON_ERROR_STOP on
BEGIN;

CREATE FUNCTION pg_temp.bits(VARIADIC p integer[]) RETURNS bigint
LANGUAGE sql IMMUTABLE AS $$ SELECT bit_or(1::bigint << b) FROM unnest(p) AS b $$;

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'security_level: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

-- case, crypt, crypt_bits, mfp_sup, mfp_req -> kind, level, label
CREATE TEMP TABLE cases (name text, crypt text, bits bigint, mfp_sup boolean, mfp_req boolean,
                         kind text, level text, label text);
INSERT INTO cases VALUES
  -- live combinations (2026-10-11)
  ('live wpa2-psk',        'WPA2 WPA2-PSK AES-CCMP', x'400400040a'::bigint, false, false, 'psk', 'psk_no_mfp', 'WPA2-PSK'),
  ('live no record',       '', 0, false, false, 'unknown', 'unknown', 'Unknown'),
  ('live transition',      'WPA3 WPA3-PSK WPA3-SAE AES-CCMP', x'10400400041a'::bigint, true, false,
                           'transition', 'psk_no_mfp', 'WPA2/WPA3 transition'),
  ('live open',            'Open', 0, false, false, 'open', 'open', 'Open'),
  ('live psk mfp capable', 'WPA2 WPA2-PSK AES-CCMP', x'400400040a'::bigint, true, false, 'psk', 'psk_no_mfp', 'WPA2-PSK'),
  ('live wpa1/wpa2 tkip',  'WPA2 WPA2-PSK TKIP AES-CCMP', x'400600020e'::bigint, false, false,
                           'wpa1_mixed', 'legacy', 'WPA1/WPA2 mixed'),
  ('live enterprise ft',   'WPA2 WPA2-EAP WPA2-EAP-FT AES-CCMP', x'a00400040a'::bigint, false, false,
                           'enterprise', 'strong', 'WPA2-Enterprise'),
  ('live bits unknown',    'WPA2 WPA2-PSK AES-CCMP', 0, false, false, 'psk', 'psk_no_mfp', 'WPA2-PSK'),
  ('live wpa1/wpa2 ccmp',  'WPA2 WPA2-PSK AES-CCMP', x'400400040e'::bigint, false, false,
                           'wpa1_mixed', 'legacy', 'WPA1/WPA2 mixed'),
  ('live transition 256',  'WPA3 WPA3-PSK WPA3-PSK WPA3-SAE AES-CCMP', x'14400400041a'::bigint, true, false,
                           'transition', 'psk_no_mfp', 'WPA2/WPA3 transition'),
  ('live tkip next to ccmp', 'WPA2 WPA2-PSK TKIP AES-CCMP', x'400600020a'::bigint, false, false,
                           'tkip', 'legacy', 'WPA2 (TKIP)'),
  ('live transition, bits unknown', 'WPA3 WPA3-PSK WPA3-SAE AES-CCMP', 0, false, false,
                           'transition', 'psk_no_mfp', 'WPA2/WPA3 transition'),
  ('live wpa1/wpa2 group tkip', 'WPA2 WPA2-PSK TKIP AES-CCMP', x'400400020e'::bigint, false, false,
                           'wpa1_mixed', 'legacy', 'WPA1/WPA2 mixed'),
  -- synthetic
  ('wep40',                'WEP40', pg_temp.bits(0, 7, 23), false, false, 'wep', 'legacy', 'WEP'),
  ('wep104',               'WEP104', pg_temp.bits(0, 8, 24), false, false, 'wep', 'legacy', 'WEP'),
  ('wpa1 tkip only',       'WPA1 WPA1-PSK TKIP', pg_temp.bits(1, 2, 9, 25, 38), false, false, 'wpa1', 'legacy', 'WPA1'),
  ('wpa2 tkip only',       'WPA2 WPA2-PSK TKIP', pg_temp.bits(1, 3, 9, 25, 38), false, false, 'tkip', 'legacy', 'WPA2 (TKIP)'),
  ('psk mfp required',     'WPA2 WPA2-PSK AES-CCMP', pg_temp.bits(1, 3, 10, 26, 38), true, true, 'psk', 'strong', 'WPA2-PSK'),
  ('psk-sha256',           'WPA2 WPA2-PSK AES-CCMP', pg_temp.bits(1, 3, 10, 26, 42), true, false, 'psk', 'psk_no_mfp', 'WPA2-PSK'),
  ('ft-psk',               'WPA2 WPA2-PSK AES-CCMP', pg_temp.bits(1, 3, 10, 26, 40), false, false, 'psk', 'psk_no_mfp', 'WPA2-PSK'),
  ('transition mfp req',   'WPA3 WPA3-PSK WPA3-SAE AES-CCMP', pg_temp.bits(1, 3, 4, 10, 26, 38, 44), true, true,
                           'transition', 'strong', 'WPA2/WPA3 transition'),
  ('wpa3-sae only',        'WPA3 WPA3-SAE AES-CCMP', pg_temp.bits(1, 3, 4, 10, 26, 44), true, true, 'sae', 'strong', 'WPA3-SAE'),
  ('wpa2-eap',             'WPA2 WPA2-EAP AES-CCMP', pg_temp.bits(1, 3, 10, 26, 37), false, false, 'enterprise', 'strong', 'WPA2-Enterprise'),
  ('wpa3-enterprise 192',  'WPA3 WPA3-EAP GCMP-256', pg_temp.bits(1, 3, 4, 13, 29, 48), true, true,
                           'enterprise3', 'strong', 'WPA3-Enterprise'),
  ('psk and 802.1x',       'WPA2 WPA2-PSK WPA2-EAP AES-CCMP', pg_temp.bits(1, 3, 10, 26, 37, 38), false, false,
                           'psk', 'psk_no_mfp', 'WPA2-PSK'),
  ('owe',                  'WPA3 WPA3-SAE AES-CCMP', pg_temp.bits(1, 4, 10, 26, 54), true, true, 'owe', 'strong', 'OWE'),
  -- the OWE-transition vendor IE ORs Kismet's legacy crypt_wpa_owe (1 << 17)
  -- into the set: an open BSS that shows bit 17 alone
  ('owe transition quirk', '', pg_temp.bits(17), false, false, 'open', 'open', 'Open'),
  ('only wpa from data',   'WPA', pg_temp.bits(1), false, false, 'unknown', 'unknown', 'Unknown'),
  ('null bits, open',      'Open', NULL, NULL, NULL, 'open', 'open', 'Open'),
  ('null everything',      NULL, NULL, NULL, NULL, 'unknown', 'unknown', 'Unknown'),
  ('bits unknown, eap',    'WPA2 WPA2-EAP AES-CCMP', 0, false, false, 'enterprise', 'strong', 'WPA2-Enterprise'),
  ('bits unknown, wep',    'WEP104', 0, false, false, 'wep', 'legacy', 'WEP'),
  ('bits unknown, tkip',   'WPA2 WPA2-PSK TKIP AES-CCMP', 0, false, false, 'tkip', 'legacy', 'WPA2 (TKIP)');

SELECT pg_temp.check_rows('kind, level, label',
  'SELECT name, crypt_kind(crypt, bits), security_level(crypt, bits, mfp_sup, mfp_req), security_label(crypt, bits) FROM cases',
  'SELECT name, kind, level, label FROM cases');

-- security_meets(policy, require_mfp, crypt, bits, mfp_req): NULL = met
SELECT pg_temp.check_rows('security_meets', $$
  SELECT p, r, c.name, security_meets(p, r, c.crypt, c.bits, c.mfp_req)
  FROM cases c, (VALUES ('enterprise', true), ('enterprise', false), ('psk_or_better', false),
                        ('wpa3', false), ('open_allowed', false), ('open_allowed', true)) AS x(p, r)
  WHERE c.name IN ('live enterprise ft', 'live wpa2-psk', 'live transition', 'live open',
                   'wpa3-sae only', 'psk mfp required', 'owe', 'live no record', 'wpa2-eap')$$, $$
  VALUES
    -- the board's case: Enterprise policy with MFP required, AP Enterprise with MFP not required
    ('enterprise', true, 'live enterprise ft', 'MFP not required'),
    ('enterprise', true, 'wpa2-eap', 'MFP not required'),
    ('enterprise', true, 'live wpa2-psk', 'not Enterprise'),
    ('enterprise', true, 'live transition', 'not Enterprise'),
    ('enterprise', true, 'live open', 'not Enterprise'),
    ('enterprise', true, 'wpa3-sae only', 'not Enterprise'),
    ('enterprise', true, 'psk mfp required', 'not Enterprise'),
    ('enterprise', true, 'owe', 'not Enterprise'),
    ('enterprise', true, 'live no record', 'security unknown'),
    ('enterprise', false, 'live enterprise ft', NULL),
    ('enterprise', false, 'wpa2-eap', NULL),
    ('enterprise', false, 'live wpa2-psk', 'not Enterprise'),
    ('enterprise', false, 'live transition', 'not Enterprise'),
    ('enterprise', false, 'live open', 'not Enterprise'),
    ('enterprise', false, 'wpa3-sae only', 'not Enterprise'),
    ('enterprise', false, 'psk mfp required', 'not Enterprise'),
    ('enterprise', false, 'owe', 'not Enterprise'),
    ('enterprise', false, 'live no record', 'security unknown'),
    ('psk_or_better', false, 'live enterprise ft', NULL),
    ('psk_or_better', false, 'wpa2-eap', NULL),
    ('psk_or_better', false, 'live wpa2-psk', NULL),
    ('psk_or_better', false, 'live transition', NULL),
    ('psk_or_better', false, 'live open', 'weaker than WPA2-PSK'),
    ('psk_or_better', false, 'wpa3-sae only', NULL),
    ('psk_or_better', false, 'psk mfp required', NULL),
    ('psk_or_better', false, 'owe', 'weaker than WPA2-PSK'),
    ('psk_or_better', false, 'live no record', 'security unknown'),
    ('wpa3', false, 'live enterprise ft', 'not WPA3'),
    ('wpa3', false, 'wpa2-eap', 'not WPA3'),
    ('wpa3', false, 'live wpa2-psk', 'not WPA3'),
    ('wpa3', false, 'live transition', 'not WPA3'),
    ('wpa3', false, 'live open', 'not WPA3'),
    ('wpa3', false, 'wpa3-sae only', NULL),
    ('wpa3', false, 'psk mfp required', 'not WPA3'),
    ('wpa3', false, 'owe', 'not WPA3'),
    ('wpa3', false, 'live no record', 'security unknown'),
    ('open_allowed', false, 'live enterprise ft', NULL),
    ('open_allowed', false, 'wpa2-eap', NULL),
    ('open_allowed', false, 'live wpa2-psk', NULL),
    ('open_allowed', false, 'live transition', NULL),
    ('open_allowed', false, 'live open', NULL),
    ('open_allowed', false, 'wpa3-sae only', NULL),
    ('open_allowed', false, 'psk mfp required', NULL),
    ('open_allowed', false, 'owe', NULL),
    ('open_allowed', false, 'live no record', NULL),
    ('open_allowed', true, 'live enterprise ft', 'MFP not required'),
    ('open_allowed', true, 'wpa2-eap', 'MFP not required'),
    ('open_allowed', true, 'live wpa2-psk', 'MFP not required'),
    ('open_allowed', true, 'live transition', 'MFP not required'),
    ('open_allowed', true, 'live open', NULL),
    ('open_allowed', true, 'wpa3-sae only', NULL),
    ('open_allowed', true, 'psk mfp required', NULL),
    ('open_allowed', true, 'owe', NULL),
    ('open_allowed', true, 'live no record', NULL) $$);

-- the small helpers
SELECT pg_temp.check_rows('helpers', $$
  SELECT 'detail', security_detail(x'400400040a'::bigint)::text
  UNION ALL SELECT 'detail 0', coalesce(security_detail(0)::text, 'NULL')
  UNION ALL SELECT 'mfp', concat_ws(',', mfp_state(true, true), mfp_state(true, false), mfp_state(false, false),
                                    coalesce(mfp_state(NULL, NULL), 'NULL'))
  UNION ALL SELECT 'rank', concat_ws(',', security_rank('open'), security_rank('legacy'), security_rank('psk_no_mfp'),
                                     security_rank('strong'), coalesce(security_rank('unknown')::text, 'NULL'))
  UNION ALL SELECT 'kismet', concat_ws(',', kismet_severity(0::smallint), kismet_severity(5::smallint),
                                       kismet_severity(10::smallint), kismet_severity(15::smallint),
                                       kismet_severity(20::smallint), kismet_severity(NULL))$$, $$
  VALUES ('detail', '{"akm": ["PSK"], "group": ["CCMP"], "general": ["WPA", "WPA2"], "pairwise": ["CCMP"]}'),
         ('detail 0', 'NULL'),
         ('mfp', 'required,capable,no,NULL'),
         ('rank', '0,1,2,3,NULL'),
         ('kismet', 'info,low,medium,high,critical,info') $$);

DO $$ BEGIN
  RAISE NOTICE 'security_level: PASS (13 live combinations, 20 synthetic; security_meets for 4 policies; helpers)';
END $$;

ROLLBACK;
