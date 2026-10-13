-- GET /api/v2/settings/* (the stored rows) and the configured networks the
-- Networks page lists even when none of their BSSIDs was seen.

-- name: networks
SELECT n.id, n.ssid, n.kind, n.expected_security, n.require_mfp,
       ARRAY(SELECT upper(b::text) FROM unnest(n.allowed_bssids) AS b ORDER BY 1) AS allowed_bssids,
       ARRAY(SELECT upper(left(trunc(o)::text, 8)) FROM unnest(n.allowed_ouis) AS o ORDER BY 1) AS allowed_ouis,
       n.note, n.created_at, n.updated_at
FROM networks n
WHERE n.account_id = %(account_id)s
ORDER BY n.kind, n.ssid

-- name: account_settings
SELECT st.ignore_random_bssid, st.suspicious_unallowed, st.managed_by_vendor, st.ignore_weaker_dbm,
       st.mobile_vendors, st.deauth_min_bursts, st.deauth_window_s, st.evil_twin_k, st.evil_twin_min_db,
       st.evil_twin_guard_s, st.sensor_offline_min, st.updated_at
FROM account_settings st
WHERE st.account_id = %(account_id)s

-- name: sensor_config
-- Every capture configuration version of the account's sensors, newest
-- first (the newest is the desired one; confirmed_at NULL = not applied yet).
SELECT se.name AS sensor, c.version, c.channel_plan, c.channels, c.n_channels, c.dwell_s, c.poll_s,
       c.created_at, c.created_by, c.confirmed_at, c.note
FROM sensor_config c
JOIN sensors se ON se.id = c.sensor_id AND se.account_id = %(account_id)s
ORDER BY se.id, c.version DESC

-- name: sensor_history
-- The documented changes of the account's sensors, newest first; boundary =
-- a data boundary (a channel-plan change splits measurements for analysis).
SELECT se.name AS sensor, a.starts_at AS ts, a.boundary, a.label, a.ref
FROM sensor_annotations a
JOIN sensors se ON se.id = a.sensor_id AND se.account_id = %(account_id)s
WHERE a.kind = 'change'
ORDER BY a.starts_at DESC

-- name: sensor_meta
-- The editable details of the account's sensors (Settings > Sensors).
SELECT se.id, se.name, se.location, se.description, se.hardware
FROM sensors se
WHERE se.account_id = %(account_id)s
ORDER BY se.id

-- name: hardware
-- The capture hardware catalog; used = rows of sensor_hardware naming it (a
-- used row cannot be deleted).
SELECT h.id, h.kind, h.name, h.gain_dbi, h.bands, h.connector, h.note, h.created_at,
       (SELECT count(*) FROM sensor_hardware x WHERE x.adapter_id = h.id OR x.antenna_id = h.id)::int AS used
FROM capture_hardware h
WHERE h.account_id = %(account_id)s
ORDER BY h.kind, h.name

-- name: sensor_hardware
-- What the account's sensors had attached since when, newest first.
SELECT se.name AS sensor, x.valid_from, x.adapter_id, x.antenna_id, x.created_by, x.note, x.created_at
FROM sensor_hardware x
JOIN sensors se ON se.id = x.sensor_id AND se.account_id = %(account_id)s
ORDER BY se.id, x.valid_from DESC

-- name: classification_affected
-- What the rules act on today: the account's APs, of them with a randomized
-- (locally administered) BSSID - phone hotspots, "ignored" by the first rule.
SELECT count(DISTINCT x.ap_id)::int AS aps,
       count(DISTINCT x.ap_id) FILTER (WHERE su.random_bssid)::int AS random_bssid
FROM ap_sensors x
JOIN aps a ON a.id = x.ap_id AND a.account_id = %(account_id)s
JOIN ap_summary su ON su.sensor_id = x.sensor_id AND su.device_key = x.device_key

-- name: oui_vendors
-- The vendor of each OUI the account's networks allow, from the manufacturer
-- of the account's APs with that prefix (the tags of the Networks tab).
SELECT DISTINCT ON (o) upper(left(o::text, 8)) AS oui, d.manuf
FROM networks n
CROSS JOIN LATERAL unnest(n.allowed_ouis) AS o
JOIN devices d ON trunc(d.mac) = o AND d.ever_ap AND d.manuf IS NOT NULL
               AND d.sensor_id IN (SELECT s.id FROM sensors s WHERE s.account_id = %(account_id)s)
WHERE n.account_id = %(account_id)s
ORDER BY o, d.manuf

-- name: sensor_data_tables
-- The tables with a sensor_id column (not its configuration: sensor_config,
-- sensor_annotations, sensor_hardware) and whether one has a row of the sensor;
-- the guard of "Delete sensor": a sensor with any observation, poll, device
-- or rollup row is never deleted. Table names come from the catalog and are
-- quoted as identifiers by format().
SELECT t.table_name,
       (xpath('/row/c/text()', query_to_xml(
          format('SELECT count(*) AS c FROM (SELECT 1 FROM public.%%I WHERE sensor_id = %%s LIMIT 1) x',
                 t.table_name, %(sensor_id)s::int), false, true, '')))[1]::text::int AS has_rows
FROM information_schema.tables t
WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
  AND EXISTS (SELECT 1 FROM information_schema.columns c
              WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name AND c.column_name = 'sensor_id')
  AND t.table_name NOT IN ('sensor_config', 'sensor_annotations', 'sensor_hardware')
ORDER BY t.table_name
