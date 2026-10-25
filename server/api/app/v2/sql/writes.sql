-- The write statements of /api/v2 (PLAN.md 7.5; routes/settings.py,
-- routes/events.py). Each runs inside deps.write_tx() - one transaction with
-- its audit_log rows and accounts.write_version + 1. Not a read group:
-- tests/perf/ep_v2.py never loads this file (it would EXPLAIN ANALYZE them).

-- name: network_insert
INSERT INTO networks (account_id, ssid, kind, expected_security, require_mfp, allowed_bssids, allowed_ouis, note)
VALUES (%(account_id)s, %(ssid)s, %(kind)s, %(expected_security)s, %(require_mfp)s,
        %(allowed_bssids)s::macaddr[], %(allowed_ouis)s::macaddr[], %(note)s)
RETURNING id

-- name: network_update
UPDATE networks SET ssid = %(ssid)s, kind = %(kind)s, expected_security = %(expected_security)s,
       require_mfp = %(require_mfp)s, allowed_bssids = %(allowed_bssids)s::macaddr[],
       allowed_ouis = %(allowed_ouis)s::macaddr[], note = %(note)s, updated_at = now()
WHERE account_id = %(account_id)s AND id = %(id)s
RETURNING id

-- name: network_delete
DELETE FROM networks WHERE account_id = %(account_id)s AND id = %(id)s RETURNING id

-- name: network_allow_bssid
-- "Add to allowed BSSIDs": appended once
UPDATE networks SET allowed_bssids = (SELECT array_agg(DISTINCT b ORDER BY b)
                                      FROM unnest(allowed_bssids || %(value)s::macaddr) AS b),
       updated_at = now()
WHERE account_id = %(account_id)s AND id = %(id)s
RETURNING id

-- name: network_allow_oui
UPDATE networks SET allowed_ouis = (SELECT array_agg(DISTINCT o ORDER BY o)
                                    FROM unnest(allowed_ouis || trunc(%(value)s::macaddr)) AS o),
       updated_at = now()
WHERE account_id = %(account_id)s AND id = %(id)s
RETURNING id

-- name: classification_update
UPDATE account_settings SET ignore_random_bssid = %(ignore_random_bssid)s,
       suspicious_unallowed = %(suspicious_unallowed)s, managed_by_vendor = %(managed_by_vendor)s,
       ignore_weaker_dbm = %(ignore_weaker_dbm)s, mobile_vendors = %(mobile_vendors)s::text[], updated_at = now()
WHERE account_id = %(account_id)s
RETURNING account_id

-- name: detectors_update
UPDATE account_settings SET deauth_min_bursts = %(deauth_min_bursts)s, deauth_window_s = %(deauth_window_s)s,
       evil_twin_k = %(evil_twin_k)s, evil_twin_min_db = %(evil_twin_min_db)s,
       evil_twin_guard_s = %(evil_twin_guard_s)s, sensor_offline_min = %(sensor_offline_min)s, updated_at = now()
WHERE account_id = %(account_id)s
RETURNING account_id

-- name: sensor_config_insert
-- A new desired capture configuration (confirmed_at NULL until the sensor
-- applied it; schema 18: with the sha256 the sensor checks - docs/config-delivery.md)
INSERT INTO sensor_config (sensor_id, version, channel_plan, channels, n_channels, dwell_s, poll_s, created_by, note,
                           sha256)
SELECT %(sensor_id)s, coalesce(max(c.version), 0) + 1, %(channel_plan)s, %(channels)s::text[], %(n_channels)s,
       %(dwell_s)s, %(poll_s)s, 'operator', %(note)s, %(sha256)s
FROM sensor_config c WHERE c.sensor_id = %(sensor_id)s
RETURNING version

-- name: sensor_insert
-- A new, empty sensor of the account (its day boundaries in the account's zone).
INSERT INTO sensors (account_id, name, location, description, tz)
VALUES (%(account_id)s, %(name)s, %(location)s, %(description)s, %(tz)s)
RETURNING id

-- name: sensor_update
-- Name, location and description; sensor_id is the key everything references,
-- the name only a unique label (a NULL name keeps it).
UPDATE sensors SET name = coalesce(%(name)s, name), location = %(location)s, description = %(description)s
WHERE account_id = %(account_id)s AND id = %(id)s
RETURNING id

-- name: sensor_delete_config
-- The configuration rows of a sensor without data, with the sensor (the only
-- rows that go with it - sensor_data_tables found nothing else); the history
-- entries include its hardware changes; its ingest tokens are revoked by
-- deleting them
WITH d AS (DELETE FROM sensor_adapters WHERE sensor_id = %(id)s RETURNING 1),
     c AS (DELETE FROM sensor_config WHERE sensor_id = %(id)s RETURNING 1),
     a AS (DELETE FROM sensor_annotations WHERE sensor_id = %(id)s RETURNING 1),
     t AS (DELETE FROM sensor_tokens WHERE sensor_id = %(id)s RETURNING id),
     -- an all-zero summary (sensor_data_tables lets only that one through)
     s AS (DELETE FROM sensor_summary WHERE sensor_id = %(id)s RETURNING 1),
     -- the refresher's log rows of the sensor (schema 16; e.g. its nightly baseline runs)
     r AS (DELETE FROM refresh_runs WHERE sensor_id = %(id)s RETURNING 1)
SELECT (SELECT count(*) FROM d)::int AS adapter, (SELECT count(*) FROM c)::int AS config,
       (SELECT count(*) FROM a)::int AS history, (SELECT count(*) FROM s)::int AS empty_summary,
       (SELECT count(*) FROM r)::int AS refresh_runs,
       -- live upload (schema 14): the sensor's ingest tokens go with it (amendment 4) -
       -- a token cannot outlive its sensor; the audit row lists their ids
       (SELECT coalesce(array_agg(id ORDER BY id), '{}') FROM t) AS tokens_revoked

-- name: sensor_delete
DELETE FROM sensors WHERE account_id = %(account_id)s AND id = %(id)s RETURNING id

-- name: hardware_change_insert
-- A hardware change of a sensor, entered by hand: a dated history entry that is
-- a data boundary (RSSI baselines never span it)
INSERT INTO sensor_annotations (sensor_id, starts_at, kind, boundary, label, ref)
VALUES (%(sensor_id)s, %(valid_from)s, 'hardware_change', true, %(label)s, NULL)
RETURNING starts_at

-- name: event_status
-- The operator's label of events of the account (schema 12: open, or one of
-- the three outcomes with its reason - the constraint refuses a mismatch;
-- schema 17: the cause of a benign / false-positive label, NULL otherwise).
-- A NULL note keeps the stored one; detectors' columns (updated_at) stay.
UPDATE detections d
SET status = %(status)s, status_reason = %(reason)s, status_cause = %(cause)s, status_at = now(),
    status_note = coalesce(%(note)s, d.status_note)
FROM sensors se
WHERE se.id = d.sensor_id AND se.account_id = %(account_id)s AND d.id = ANY (%(ids)s::bigint[])
RETURNING d.id

-- name: ap_classification
-- The manual classification of an account AP (NULL = back to the rules).
UPDATE aps SET classification = %(classification)s, classified_at = CASE WHEN %(classification)s::text IS NULL
                                                                         THEN NULL ELSE now() END
WHERE account_id = %(account_id)s AND bssid = %(bssid)s::macaddr
RETURNING id

-- name: ap_note
UPDATE aps SET note = %(note)s, note_at = CASE WHEN %(note)s::text IS NULL THEN NULL ELSE now() END
WHERE account_id = %(account_id)s AND bssid = %(bssid)s::macaddr
RETURNING id
