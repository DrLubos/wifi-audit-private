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
