-- Server schema 17 -> 18 (schema.sql, schema 18): configuration delivery to
-- the sensor (docs/config-delivery.md 9). ONE transaction: any error leaves
-- schema 17 as it is. Run through ops/migrate_schema18.sh. Nothing existing
-- changes meaning: the new columns are NULL, the new table empty; the
-- ingest functions gain polls.config_version (an uploader that does not send
-- it - 1.1, buffer v8 - stores NULL). Deploy the new api right afterwards:
-- its pooled connections recreate the staging tables with the new column.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '17' THEN
    RAISE EXCEPTION 'migrate_schema18: expected schema_version 17';
  END IF;
END $$;

\echo [1/5] sensor_config: hash, delivery, refusal
ALTER TABLE sensor_config ADD COLUMN sha256 text CONSTRAINT sensor_config_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN delivered_at timestamptz, ADD COLUMN refused_at timestamptz, ADD COLUMN refused_reason text;

\echo [2/5] sensor_config_attempts
CREATE TABLE sensor_config_attempts (
  sensor_id      integer NOT NULL REFERENCES sensors(id),
  version        integer NOT NULL,
  sha256         text NOT NULL,
  result         text NOT NULL
                 CHECK (result IN ('received', 'applying', 'applied', 'rejected', 'rolled_back', 'failed')),
  reason         text,
  started_poll   bigint,
  effective_poll bigint,
  effective_ts   timestamptz,
  restored_ts    timestamptz,
  checks         jsonb NOT NULL DEFAULT '{}',
  reported_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, version, sha256, result));

\echo [3/5] sensor_status: capabilities
ALTER TABLE sensor_status ADD COLUMN capabilities jsonb, ADD COLUMN capabilities_at timestamptz;

\echo [4/5] polls.config_version, polls_seq
ALTER TABLE polls ADD COLUMN config_version integer;
CREATE INDEX polls_seq ON polls (sensor_id, seq);

\echo [5/5] ingest functions, the duplicate merge (carries the schema-17 cause), schema_meta 18
CREATE OR REPLACE FUNCTION ingest_stage_create()
RETURNS void
LANGUAGE plpgsql
SET client_min_messages = warning       -- no "already exists, skipping" notice on every call
AS $$
BEGIN
  CREATE TEMP TABLE IF NOT EXISTS stage_devices (device_key text, mac text, type text, manuf text,
    first_seen bigint, last_seen bigint, ssid text, cloaked integer, crypt text, crypt_bits bigint,
    mfp_sup integer, mfp_req integer, adv_channel text, ht_mode text, beacon_rate integer,
    country text, config_changed_at bigint, cur_n_clients integer, cur_qbss_stations integer,
    cur_util_pct real, cur_at bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_polls (seq bigint, ts bigint, ts_offset_s real,
    kismet_ts bigint, devices_total integer, devices_active integer, new_obs integer,
    ds_running integer, ds_error text, ds_packets bigint, ds_hop_n integer,
    ds_hop_visited integer, ds_hop_ok integer, duration_ms integer, ok integer,
    error text, config_version integer) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_device_config_history (device_key text, ts bigint,
    ssid text, cloaked integer, crypt text, crypt_bits bigint, mfp_sup integer, mfp_req integer,
    adv_channel text, ht_mode text, beacon_rate integer, country text) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_observations (ts bigint, device_key text, rssi smallint,
    rssi_floor integer, pk_total bigint, pk_data bigint, disconnects integer,
    disconnects_last bigint, bss_timestamp bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_ap_load_hourly (device_key text, hour bigint,
    channel text, polls integer, clients_polls integer, clients_max integer, clients_sum integer,
    qbss_polls integer, qbss_max integer, qbss_sum integer, util_max real,
    util_sum real) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_load_samples (ts timestamptz, device_key text,
    n_clients integer, qbss_stations integer, util_pct real) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_device_freq_hist (device_key text, freq_khz integer,
    packets bigint, updated_ts bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_associations (ap_key text, client_mac text,
    first_seen bigint, last_seen bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_client_bssids (device_key text, bssid text,
    first_seen bigint, last_seen bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_probes (device_key text, ssid text, first_time bigint,
    last_time bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_alerts (hash text, ts double precision, header text,
    class text, severity smallint, source_mac text, dest_mac text, transmitter_mac text,
    other_mac text, channel text, freq_khz integer, device_key text, text text,
    raw text) ON COMMIT DELETE ROWS;
END
$$;

CREATE OR REPLACE FUNCTION ingest_merge(p_sensor_id integer, p_via text)
RETURNS jsonb
LANGUAGE plpgsql
SET work_mem = '64MB'      -- a snapshot stages ~2.5M observation rows
SET jit = off
AS $$
DECLARE
  v_staged    jsonb := '{}';
  v_inserted  jsonb := '{}';
  v_conflicts jsonb := '{}';
  v_s   bigint;
  v_n   bigint;
  v_t   timestamptz;
  v_min timestamptz;
  v_any boolean := false;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE id = p_sensor_id) THEN
    RAISE EXCEPTION 'ingest_merge: unknown sensor_id %', p_sensor_id;
  END IF;
  IF p_via IS NULL OR p_via NOT IN ('snapshot', 'ingest') THEN
    RAISE EXCEPTION 'ingest_merge: via must be snapshot or ingest, not %', p_via;
  END IF;
  PERFORM ingest_stage_create();
  PERFORM pg_advisory_xact_lock(hashtext('ingest_merge'), p_sensor_id);

  -- 1. devices (every other table references them)
  SELECT count(*) INTO v_s FROM stage_devices;
  IF v_s > 0 THEN
    INSERT INTO devices (sensor_id, device_key, mac, type, manuf, first_seen, last_seen, ssid,
                         cloaked, crypt, crypt_bits, mfp_sup, mfp_req, adv_channel, ht_mode,
                         beacon_rate, country, config_changed_at, cur_n_clients,
                         cur_qbss_stations, cur_util_pct, cur_at)
    SELECT p_sensor_id, s.device_key, s.mac::macaddr, s.type, s.manuf, to_timestamp(s.first_seen),
           to_timestamp(s.last_seen), s.ssid, s.cloaked::boolean, s.crypt, s.crypt_bits,
           s.mfp_sup::boolean, s.mfp_req::boolean, s.adv_channel, s.ht_mode, s.beacon_rate,
           s.country, to_timestamp(s.config_changed_at), s.cur_n_clients, s.cur_qbss_stations,
           s.cur_util_pct, to_timestamp(s.cur_at)
    FROM stage_devices s
    WHERE p_via = 'snapshot'
       OR ap_evidence(s.type, s.ssid, s.crypt, s.adv_channel)
       OR EXISTS (SELECT 1 FROM devices k
                  WHERE k.sensor_id = p_sensor_id AND k.device_key = s.device_key AND k.ever_ap)
    ON CONFLICT (sensor_id, device_key) DO UPDATE SET
      -- every SET expression sees the stored row, so all compare with the old last_seen
      mac = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mac ELSE devices.mac END,
      type = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.type ELSE devices.type END,
      manuf = coalesce(CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.manuf END,
                       devices.manuf, EXCLUDED.manuf),
      first_seen = least(devices.first_seen, EXCLUDED.first_seen),
      ssid = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.ssid ELSE devices.ssid END,
      cloaked = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.cloaked ELSE devices.cloaked END,
      crypt = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.crypt ELSE devices.crypt END,
      crypt_bits = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.crypt_bits ELSE devices.crypt_bits END,
      mfp_sup = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mfp_sup ELSE devices.mfp_sup END,
      mfp_req = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mfp_req ELSE devices.mfp_req END,
      adv_channel = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.adv_channel ELSE devices.adv_channel END,
      ht_mode = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.ht_mode ELSE devices.ht_mode END,
      beacon_rate = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.beacon_rate ELSE devices.beacon_rate END,
      country = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.country ELSE devices.country END,
      config_changed_at = CASE WHEN EXCLUDED.last_seen >= devices.last_seen
                               THEN coalesce(EXCLUDED.config_changed_at, devices.config_changed_at)
                               ELSE devices.config_changed_at END,
      cur_n_clients = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                           THEN EXCLUDED.cur_n_clients ELSE devices.cur_n_clients END,
      cur_qbss_stations = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                               THEN EXCLUDED.cur_qbss_stations ELSE devices.cur_qbss_stations END,
      cur_util_pct = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                          THEN EXCLUDED.cur_util_pct ELSE devices.cur_util_pct END,
      cur_at = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                    THEN EXCLUDED.cur_at ELSE devices.cur_at END,
      last_seen = greatest(devices.last_seen, EXCLUDED.last_seen);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('devices', v_s);
    v_inserted := v_inserted || jsonb_build_object('devices', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('devices', v_s - v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 2. polls
  SELECT count(*) INTO v_s FROM stage_polls;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO polls (sensor_id, ts, kismet_ts, devices_total, devices_active, new_obs, ds_running,
                         ds_error, ds_packets, ds_hop_n, ds_hop_visited, ds_hop_ok, duration_ms, ok,
                         error, seq, received_at, ts_offset_s, config_version)
      SELECT p_sensor_id, to_timestamp(s.ts), to_timestamp(s.kismet_ts), s.devices_total,
             s.devices_active, s.new_obs, s.ds_running::boolean, s.ds_error, s.ds_packets, s.ds_hop_n,
             s.ds_hop_visited, s.ds_hop_ok::boolean, s.duration_ms, s.ok::boolean, s.error, s.seq,
             now(), s.ts_offset_s, s.config_version
      FROM stage_polls s
      ORDER BY s.ts
      ON CONFLICT DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('polls', v_s);
    v_inserted := v_inserted || jsonb_build_object('polls', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('polls', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
    IF p_via = 'snapshot' THEN
      -- polls stored before schema 14 learn their buffer row id from a later snapshot
      UPDATE polls p SET seq = s.seq
      FROM stage_polls s
      WHERE p.sensor_id = p_sensor_id AND p.ts = to_timestamp(s.ts) AND p.seq IS NULL
        AND s.seq IS NOT NULL;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_inserted := v_inserted || jsonb_build_object('polls_seq_filled', v_n);
    END IF;
  END IF;

  -- 3. the configuration history (before the load samples: their channel comes from it)
  SELECT count(*) INTO v_s FROM stage_device_config_history;
  IF v_s > 0 THEN
    INSERT INTO device_config_history (sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits,
                                       mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
    SELECT p_sensor_id, s.device_key, to_timestamp(s.ts), s.ssid, s.cloaked::boolean, s.crypt,
           s.crypt_bits, s.mfp_sup::boolean, s.mfp_req::boolean, s.adv_channel, s.ht_mode,
           s.beacon_rate, s.country
    FROM stage_device_config_history s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
                  AND (p_via = 'snapshot' OR d.ever_ap)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('device_config_history', v_s);
    v_inserted := v_inserted || jsonb_build_object('device_config_history', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('device_config_history', v_s - v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 4. observations: AP rows only (devices.ever_ap, schema 10)
  SELECT count(*) INTO v_s FROM stage_observations;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO observations (ts, pk_total, pk_data, bss_timestamp, disconnects_last, sensor_id,
                                disconnects, rssi, rssi_floor, device_key)
      SELECT to_timestamp(s.ts), s.pk_total, s.pk_data, s.bss_timestamp,
             to_timestamp(s.disconnects_last), p_sensor_id, s.disconnects, s.rssi,
             s.rssi_floor::boolean, s.device_key
      FROM stage_observations s
      JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key AND d.ever_ap
      ORDER BY s.ts, s.device_key
      ON CONFLICT DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('observations', v_s);
    v_inserted := v_inserted || jsonb_build_object('observations', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('observations', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 5. AP load per hour (buffer v7): the row with more polls wins
  SELECT count(*) INTO v_s FROM stage_ap_load_hourly;
  IF v_s > 0 THEN
    INSERT INTO ap_load_hourly AS t (sensor_id, device_key, hour, channel, polls, clients_polls,
                                     clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum,
                                     util_max, util_sum, source)
    SELECT p_sensor_id, s.device_key, to_timestamp(s.hour), s.channel, s.polls, s.clients_polls,
           s.clients_max, s.clients_sum, s.qbss_polls, s.qbss_max, s.qbss_sum, s.util_max,
           s.util_sum, 'buffer'
    FROM stage_ap_load_hourly s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key AND d.ever_ap
    ON CONFLICT (sensor_id, device_key, hour, channel) DO UPDATE SET
      polls = EXCLUDED.polls, clients_polls = EXCLUDED.clients_polls,
      clients_max = EXCLUDED.clients_max, clients_sum = EXCLUDED.clients_sum,
      qbss_polls = EXCLUDED.qbss_polls, qbss_max = EXCLUDED.qbss_max,
      qbss_sum = EXCLUDED.qbss_sum, util_max = EXCLUDED.util_max,
      util_sum = EXCLUDED.util_sum, source = EXCLUDED.source
      WHERE EXCLUDED.polls > t.polls;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('ap_load_hourly', v_s);
    v_inserted := v_inserted || jsonb_build_object('ap_load_hourly', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 6. per-poll AP load samples (buffers <= v5; schema 8) - the same fuller-wins rule
  SELECT count(*) INTO v_s FROM stage_load_samples;
  IF v_s > 0 THEN
    SELECT coalesce(sum(l.inserted + l.updated), 0) INTO v_n FROM load_ap_load_samples(p_sensor_id) l;
    v_staged := v_staged || jsonb_build_object('load_samples', v_s);
    v_inserted := v_inserted || jsonb_build_object('load_samples', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 7. frequency histogram: the newer updated_ts wins
  SELECT count(*) INTO v_s FROM stage_device_freq_hist;
  IF v_s > 0 THEN
    INSERT INTO device_freq_hist (sensor_id, device_key, freq_khz, packets, updated_ts)
    SELECT p_sensor_id, s.device_key, s.freq_khz, s.packets, to_timestamp(s.updated_ts)
    FROM stage_device_freq_hist s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
                  AND (p_via = 'snapshot' OR d.ever_ap)
    ON CONFLICT (sensor_id, device_key, freq_khz) DO UPDATE SET
      packets = EXCLUDED.packets, updated_ts = EXCLUDED.updated_ts
      WHERE EXCLUDED.updated_ts > device_freq_hist.updated_ts;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('device_freq_hist', v_s);
    v_inserted := v_inserted || jsonb_build_object('device_freq_hist', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 8. client tables: snapshot only (live upload decision D3)
  IF p_via = 'snapshot' THEN
    SELECT count(*) INTO v_s FROM stage_associations;
    IF v_s > 0 THEN
      INSERT INTO associations (sensor_id, ap_key, client_mac, first_seen, last_seen)
      SELECT p_sensor_id, s.ap_key, s.client_mac::macaddr, to_timestamp(s.first_seen),
             to_timestamp(s.last_seen)
      FROM stage_associations s
      ON CONFLICT (sensor_id, ap_key, client_mac) DO UPDATE SET
        first_seen = least(associations.first_seen, EXCLUDED.first_seen),
        last_seen = greatest(associations.last_seen, EXCLUDED.last_seen);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('associations', v_s);
      v_inserted := v_inserted || jsonb_build_object('associations', v_n);
    END IF;
    SELECT count(*) INTO v_s FROM stage_client_bssids;
    IF v_s > 0 THEN
      INSERT INTO client_bssids (sensor_id, client_key, bssid, first_seen, last_seen)
      SELECT p_sensor_id, s.device_key, s.bssid::macaddr, to_timestamp(s.first_seen),
             to_timestamp(s.last_seen)
      FROM stage_client_bssids s
      ON CONFLICT (sensor_id, client_key, bssid) DO UPDATE SET
        first_seen = least(client_bssids.first_seen, EXCLUDED.first_seen),
        last_seen = greatest(client_bssids.last_seen, EXCLUDED.last_seen);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('client_bssids', v_s);
      v_inserted := v_inserted || jsonb_build_object('client_bssids', v_n);
    END IF;
    SELECT count(*) INTO v_s FROM stage_probes;
    IF v_s > 0 THEN
      INSERT INTO probes (sensor_id, device_key, ssid, first_time, last_time)
      SELECT p_sensor_id, s.device_key, s.ssid, to_timestamp(s.first_time), to_timestamp(s.last_time)
      FROM stage_probes s
      ON CONFLICT (sensor_id, device_key, ssid) DO UPDATE SET
        first_time = least(probes.first_time, EXCLUDED.first_time),
        last_time = greatest(probes.last_time, EXCLUDED.last_time);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('probes', v_s);
      v_inserted := v_inserted || jsonb_build_object('probes', v_n);
    END IF;
  END IF;

  -- 9. Kismet's alerts, deduplicated by hash (unchanged, D3)
  SELECT count(*) INTO v_s FROM stage_alerts;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO alerts (sensor_id, hash, ts, header, class, severity, source_mac, dest_mac,
                          transmitter_mac, other_mac, channel, freq_khz, device_key, text, raw)
      SELECT p_sensor_id, s.hash, to_timestamp(s.ts), s.header, s.class, s.severity,
             s.source_mac::macaddr, s.dest_mac::macaddr, s.transmitter_mac::macaddr,
             s.other_mac::macaddr, s.channel, s.freq_khz, s.device_key, s.text, s.raw::jsonb
      FROM stage_alerts s
      ON CONFLICT (sensor_id, hash) DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('alerts', v_s);
    v_inserted := v_inserted || jsonb_build_object('alerts', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('alerts', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 10. the refresher's work: rollups from the earliest new ts (a device-only
  --     change: the current hour and the whole-sensor steps)
  IF v_any THEN
    INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at)
    VALUES (p_sensor_id, coalesce(v_min, now()), now(), now())
    ON CONFLICT (sensor_id) DO UPDATE SET
      dirty_from = least(i.dirty_from, EXCLUDED.dirty_from),
      dirty_since = coalesce(i.dirty_since, EXCLUDED.dirty_since),
      dirty_at = EXCLUDED.dirty_at;
  END IF;

  RETURN jsonb_build_object('via', p_via, 'staged', v_staged, 'inserted', v_inserted,
                            'conflicts', v_conflicts, 'min_ts', v_min);
END
$$;

CREATE OR REPLACE FUNCTION merge_duplicate_kismet_events(p_sensor_id integer)
RETURNS TABLE (kept_id bigint, merged_id bigint, alert_header text, new_status text, new_note text)
LANGUAGE plpgsql AS $$
DECLARE
  v_account   integer;
  v_status    text;
  v_status_at timestamptz;
  v_reason    text;
  v_cause     text;                          -- schema 17
  v_note      text;
  m           record;
BEGIN
  SELECT account_id INTO v_account FROM sensors WHERE id = p_sensor_id;
  IF v_account IS NULL THEN
    RAISE EXCEPTION 'merge_duplicate_kismet_events: unknown sensor_id %', p_sensor_id;
  END IF;
  FOR m IN
    SELECT k.id AS k_id, k.status AS k_status, k.status_at AS k_at, k.status_note AS k_note,
           k.status_reason AS k_reason, u.status_reason AS u_reason,
           k.status_cause AS k_cause, u.status_cause AS u_cause,
           u.id AS u_id, u.status AS u_status, u.status_at AS u_at, u.status_note AS u_note, u.type AS header
    FROM detections u
    JOIN detections k ON k.sensor_id = u.sensor_id AND k.kind = 'kismet' AND k.type = u.type
                     AND k.ts = u.ts AND k.ts_end IS NOT DISTINCT FROM u.ts_end AND k.mac IS NOT NULL
    WHERE u.sensor_id = p_sensor_id AND u.kind = 'kismet' AND u.device_key IS NULL AND u.mac IS NULL
      -- two APs with the same window: ambiguous, left alone
      AND (SELECT count(*) FROM detections t
           WHERE t.sensor_id = u.sensor_id AND t.kind = 'kismet' AND t.type = u.type AND t.ts = u.ts
             AND t.ts_end IS NOT DISTINCT FROM u.ts_end AND t.mac IS NOT NULL) = 1
    ORDER BY u.id
  LOOP
    -- an operator's label wins over open; of two labels the later one
    IF m.k_status = 'open' OR (m.u_status <> 'open' AND m.u_at > coalesce(m.k_at, '-infinity')) THEN
      v_status := m.u_status;
      v_status_at := m.u_at;
      v_reason := m.u_reason;
      v_cause := m.u_cause;
    ELSE
      v_status := m.k_status;
      v_status_at := m.k_at;
      v_reason := m.k_reason;
      v_cause := m.k_cause;
    END IF;
    v_note := CASE WHEN m.u_note IS NULL OR m.u_note = m.k_note THEN m.k_note
                   WHEN m.k_note IS NULL THEN m.u_note
                   ELSE m.k_note || E'\n' || m.u_note END;
    UPDATE detections d SET status = v_status, status_at = v_status_at, status_reason = v_reason,
                            status_cause = v_cause, status_note = v_note
    WHERE d.id = m.k_id
      AND (d.status, d.status_at, d.status_reason, d.status_cause, d.status_note)
          IS DISTINCT FROM (v_status, v_status_at, v_reason, v_cause, v_note);
    INSERT INTO audit_log (account_id, action, target, old, new)
    VALUES (v_account, 'event.merge', 'event:' || m.k_id,
            jsonb_build_object(
              'kept', jsonb_build_object('id', m.k_id, 'status', m.k_status, 'status_at', m.k_at,
                                         'status_reason', m.k_reason, 'status_cause', m.k_cause,
                                         'status_note', m.k_note),
              'merged', jsonb_build_object('id', m.u_id, 'status', m.u_status, 'status_at', m.u_at,
                                           'status_reason', m.u_reason, 'status_cause', m.u_cause,
                                           'status_note', m.u_note)),
            jsonb_build_object('id', m.k_id, 'status', v_status, 'status_at', v_status_at,
                               'status_reason', v_reason, 'status_cause', v_cause, 'status_note', v_note));
    DELETE FROM detections d WHERE d.id = m.u_id;
    kept_id := m.k_id;
    merged_id := m.u_id;
    alert_header := m.header;
    new_status := v_status;
    new_note := v_note;
    RETURN NEXT;
  END LOOP;
END
$$;

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '18')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
