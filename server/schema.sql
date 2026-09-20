-- wifi-audit server schema (PostgreSQL 14+, no extensions).
--
-- Idempotent: run as often as you like with
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
--
-- Mirrors the sensor's SQLite buffer (wifi-sensor/collector/store.py, schema v2)
-- with a sensor_id on every row, and adds sensors, ap_baselines and detections.
-- Differences from the buffer, all intentional:
--   * unix seconds -> timestamptz, MAC strings -> macaddr, 0/1 -> boolean,
--     alerts.raw -> jsonb; the buffer's `key` columns are named device_key here.
--   * sensor-local state is not mirrored: the `id` serials, the `sent` upload
--     flags and the `meta` table.
--   * natural keys everywhere; observations is keyed (ts, sensor_id, device_key)
--     so it converts to a TimescaleDB hypertable without a rewrite (see the end).
-- Privacy: no column for client IP addresses or WPS identity fields exists and
-- none may be added (see CLAUDE.md).

BEGIN;

CREATE TABLE IF NOT EXISTS schema_meta (
  key   text PRIMARY KEY,
  value text NOT NULL);
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- --- sensors ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sensors (
  id          serial PRIMARY KEY,
  name        text NOT NULL UNIQUE,          -- short handle, e.g. 'pi-fri'
  description text,
  location    text,                          -- free text, no coordinates
  tz          text NOT NULL DEFAULT 'UTC',   -- day boundaries for daily statistics
  created_at  timestamptz NOT NULL DEFAULT now());
COMMENT ON TABLE sensors IS
  'One row per fixed sensor. Every time-series and device row references it.';

-- --- mirrored collector tables ----------------------------------------------------

CREATE TABLE IF NOT EXISTS polls (
  sensor_id      integer NOT NULL REFERENCES sensors(id),
  ts             timestamptz NOT NULL,       -- collector time
  kismet_ts      timestamptz,                -- kismet.system.timestamp.sec
  devices_total  integer,                    -- kismet.system.devices.count
  devices_active integer,                    -- devices returned by the last-time query
  new_obs        integer,                    -- observation rows written by that poll
  ds_running     boolean,                    -- every datasource running
  ds_error       text,                       -- first datasource error reason
  ds_packets     bigint,                     -- sum of datasource num_packets
  duration_ms    integer,
  ok             boolean NOT NULL DEFAULT true,
  error          text,
  PRIMARY KEY (sensor_id, ts));
COMMENT ON TABLE polls IS
  'One row per collector poll (30 s): collector, Kismet and datasource health.';

CREATE TABLE IF NOT EXISTS devices (
  sensor_id  integer NOT NULL REFERENCES sensors(id),
  device_key text NOT NULL,                  -- Kismet device key (instance-local)
  mac        macaddr NOT NULL,
  type       text NOT NULL,                  -- ap | client | bridged | adhoc | wds | device | unknown
  manuf      text,
  first_seen timestamptz NOT NULL,           -- Kismet first_time
  last_seen  timestamptz NOT NULL,           -- Kismet last_time of the newest observation
  -- advertised AP configuration, last known values (NULL for non-APs)
  ssid        text,                          -- '' = cloaked SSID
  cloaked     boolean,
  crypt       text,                          -- Kismet crypt_string, e.g. 'WPA2-PSK CCMP'
  crypt_bits  bigint,                        -- Kismet crypt_bitfield (uint64)
  mfp_sup     boolean,
  mfp_req     boolean,
  adv_channel text,
  ht_mode     text,
  beacon_rate integer,
  country     text,
  config_changed_at timestamptz,             -- poll ts of the last configuration change
  PRIMARY KEY (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS devices_mac       ON devices (sensor_id, mac);
CREATE INDEX IF NOT EXISTS devices_type      ON devices (sensor_id, type);
CREATE INDEX IF NOT EXISTS devices_ssid      ON devices (sensor_id, ssid);
CREATE INDEX IF NOT EXISTS devices_last_seen ON devices (sensor_id, last_seen);
COMMENT ON TABLE devices IS
  'Current identity of every device the sensor has seen and, for APs, the advertised configuration. Never pruned.';

CREATE TABLE IF NOT EXISTS device_config_history (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  ts         timestamptz NOT NULL,           -- poll ts at which the NEW config was seen
  -- the configuration that was replaced
  ssid        text,
  cloaked     boolean,
  crypt       text,
  crypt_bits  bigint,
  mfp_sup     boolean,
  mfp_req     boolean,
  adv_channel text,
  ht_mode     text,
  beacon_rate integer,
  country     text,
  PRIMARY KEY (sensor_id, device_key, ts),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS device_config_history_ts ON device_config_history (sensor_id, ts);
COMMENT ON TABLE device_config_history IS
  'Previous AP configuration, appended when the advertised configuration changes. ie_checksum/beacon_fp are NOT part of the diff (they vary per beacon).';

CREATE TABLE IF NOT EXISTS observations (
  ts         timestamptz NOT NULL,           -- poll ts
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  last_time  timestamptz NOT NULL,           -- Kismet last_time
  freq_khz   integer,                        -- heard frequency
  channel    text,                           -- Kismet last known channel
  rssi       smallint,                       -- Kismet sig_last (one frame), NULL when no reading
  rssi_min   smallint,                       -- Kismet lifetime extremes
  rssi_max   smallint,
  pk_total   bigint,                         -- cumulative counters as Kismet reports them
  pk_tx      bigint,
  pk_rx      bigint,
  pk_data    bigint,
  bytes      bigint,
  -- AP only (NULL otherwise)
  n_clients     integer,
  disconnects   integer,                     -- client_disconnects: deauth/disassoc seen
  qbss_stations integer,                     -- AP-reported station count
  util_pct      real,                        -- AP-reported channel utilisation
  bss_timestamp bigint,                      -- AP uptime (us); a drop means a restart
  ie_checksum   bigint,                      -- beacon IE checksum (varies per beacon)
  beacon_fp     bigint,                      -- beacon fingerprint
  -- client only
  bssid      macaddr,
  PRIMARY KEY (ts, sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
-- The per-device time-series query; the PK is time-leading for the hypertable.
CREATE INDEX IF NOT EXISTS observations_device_ts ON observations (sensor_id, device_key, ts);
-- Covering index for the per-AP RSSI series (/api/aps/{key}/rssi): the bucket
-- query needs only (ts, rssi) of one device, so with rssi INCLUDEd it runs as
-- an index-only scan. Without it every bucket query touched ~14k heap pages
-- (rows of one AP are spread over the whole table, inserted in time order),
-- 3-5 s on a cold cache.
CREATE INDEX IF NOT EXISTS observations_device_ts_rssi
  ON observations (sensor_id, device_key, ts) INCLUDE (rssi);
COMMENT ON TABLE observations IS
  'One row per active device per poll: the core RSSI/counter time series. A row exists only for polls in which the device was active.';

CREATE TABLE IF NOT EXISTS device_freq_hist (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  freq_khz   integer NOT NULL,
  packets    bigint NOT NULL,                -- cumulative, as reported by Kismet
  updated_ts timestamptz NOT NULL,
  PRIMARY KEY (sensor_id, device_key, freq_khz),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));

CREATE TABLE IF NOT EXISTS associations (
  sensor_id  integer NOT NULL,
  ap_key     text NOT NULL,                  -- device_key of the AP
  client_mac macaddr NOT NULL,
  first_seen timestamptz NOT NULL,           -- poll ts at which the pair was first listed
  last_seen  timestamptz NOT NULL,           -- poll ts at which the AP still listed the client
  PRIMARY KEY (sensor_id, ap_key, client_mac),
  FOREIGN KEY (sensor_id, ap_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS associations_client ON associations (sensor_id, client_mac);
COMMENT ON TABLE associations IS
  'AP x client MAC pairs from Kismet''s associated_client_map. last_seen means "still listed at that poll", not "last frame exchanged".';

CREATE TABLE IF NOT EXISTS probes (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,                  -- client device
  ssid       text NOT NULL,                  -- '' = wildcard probe
  first_time timestamptz NOT NULL,
  last_time  timestamptz NOT NULL,
  PRIMARY KEY (sensor_id, device_key, ssid),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS probes_ssid ON probes (sensor_id, ssid);

CREATE TABLE IF NOT EXISTS alerts (
  id              bigserial PRIMARY KEY,
  sensor_id       integer NOT NULL REFERENCES sensors(id),
  hash            text NOT NULL,             -- Kismet alert hash (or sha1: derived by the sensor)
  ts              timestamptz,
  header          text NOT NULL,             -- e.g. DEAUTHFLOOD, CHANCHANGE
  class           text,
  severity        smallint,
  source_mac      macaddr,
  dest_mac        macaddr,
  transmitter_mac macaddr,
  other_mac       macaddr,
  channel         text,
  freq_khz        integer,
  device_key      text,                      -- no FK: Kismet may name a device the collector never stored
  text            text,
  raw             jsonb NOT NULL,            -- complete Kismet alert
  UNIQUE (sensor_id, hash));
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (sensor_id, ts);
COMMENT ON TABLE alerts IS 'Kismet''s native WIDS alerts, deduplicated by hash per sensor.';

-- --- server additions ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ap_baselines (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  -- known identity: the BSSID <-> SSID pair this baseline was learned under
  bssid      macaddr NOT NULL,
  ssid       text,                           -- '' = hidden
  crypt      text,                           -- known-good encryption string
  trusted    boolean NOT NULL DEFAULT false, -- operator-approved (frozen identity)
  trusted_at timestamptz,
  note       text,
  -- RSSI statistics as seen by this sensor (dBm)
  rssi_median    real,
  rssi_mean      real,
  rssi_sd        real,                       -- population std dev
  rssi_robust_sd real,                       -- 1.4826 * MAD
  rssi_p5        smallint,
  rssi_p95       smallint,
  main_freq_khz  integer,                    -- most frequent heard frequency
  n_obs          integer,                    -- readings with an RSSI
  n_days         integer,                    -- distinct days (in sensors.tz) with a reading
  window_start   timestamptz,
  window_end     timestamptz,
  computed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS ap_baselines_ssid  ON ap_baselines (sensor_id, ssid);
CREATE INDEX IF NOT EXISTS ap_baselines_bssid ON ap_baselines (sensor_id, bssid);
COMMENT ON TABLE ap_baselines IS
  'Per-sensor per-AP RSSI baseline (median + MAD-robust sigma, same definition as analysis/rssi_stability.py) and the known BSSID<->SSID pair. trusted = operator whitelist.';

CREATE TABLE IF NOT EXISTS detections (
  id         bigserial PRIMARY KEY,
  sensor_id  integer NOT NULL REFERENCES sensors(id),
  ts         timestamptz NOT NULL,           -- time of the evidence
  type       text NOT NULL,                  -- e.g. 'unknown_bssid_protected_ssid'
  severity   text NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
  -- subject: a stored device, or just a MAC/SSID when no device row exists
  device_key text,
  mac        macaddr,
  ssid       text,
  summary    text NOT NULL,
  evidence   jsonb NOT NULL DEFAULT '{}',
  acked      boolean NOT NULL DEFAULT false,
  acked_at   timestamptz,
  ack_note   text,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS detections_ts   ON detections (sensor_id, ts);
CREATE INDEX IF NOT EXISTS detections_open ON detections (sensor_id) WHERE NOT acked;
COMMENT ON TABLE detections IS
  'Server-side detections (none are produced yet; the table exists so the dashboard can read and acknowledge them).';

-- --- helpers ------------------------------------------------------------------------------

-- Recompute the RSSI baselines of one sensor from its observations.
-- Defaults match analysis/rssi_stability.py: an AP qualifies with >= 200
-- readings on >= 2 distinct days. p_from / p_until restrict the readings used
-- (NULL = unbounded) so a baseline can be fitted on an earlier window.
-- A trusted row keeps its ssid/crypt (the operator-approved identity); only
-- the statistics are refreshed. Returns the number of rows upserted.
CREATE OR REPLACE FUNCTION refresh_ap_baselines(
    p_sensor_id integer,
    p_min_obs   integer     DEFAULT 200,
    p_min_days  integer     DEFAULT 2,
    p_from      timestamptz DEFAULT NULL,
    p_until     timestamptz DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_tz text;
  v_n  integer;
BEGIN
  SELECT tz INTO v_tz FROM sensors WHERE id = p_sensor_id;
  IF v_tz IS NULL THEN
    RAISE EXCEPTION 'refresh_ap_baselines: unknown sensor_id %', p_sensor_id;
  END IF;

  WITH obs AS (
    SELECT o.device_key, o.ts, o.rssi, o.freq_khz,
           (o.ts AT TIME ZONE v_tz)::date AS obs_day
    FROM observations o
    JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
    WHERE o.sensor_id = p_sensor_id
      AND d.type = 'ap'
      AND o.rssi IS NOT NULL
      AND (p_from  IS NULL OR o.ts >= p_from)
      AND (p_until IS NULL OR o.ts <  p_until)
  ), stats AS (
    SELECT device_key,
           count(*)::integer                AS n_obs,
           count(DISTINCT obs_day)::integer AS n_days,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY rssi) AS med,
           avg(rssi)                        AS mean,
           stddev_pop(rssi)                 AS sd,
           percentile_cont(0.05) WITHIN GROUP (ORDER BY rssi) AS p5,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY rssi) AS p95,
           mode() WITHIN GROUP (ORDER BY freq_khz) AS main_freq,
           min(ts) AS w_start,
           max(ts) AS w_end
    FROM obs
    GROUP BY device_key
    HAVING count(*) >= p_min_obs AND count(DISTINCT obs_day) >= p_min_days
  ), mad AS (
    SELECT o.device_key,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(o.rssi - s.med)) AS mad
    FROM obs o
    JOIN stats s ON s.device_key = o.device_key
    GROUP BY o.device_key
  )
  INSERT INTO ap_baselines (
      sensor_id, device_key, bssid, ssid, crypt,
      rssi_median, rssi_mean, rssi_sd, rssi_robust_sd, rssi_p5, rssi_p95,
      main_freq_khz, n_obs, n_days, window_start, window_end, computed_at)
  SELECT p_sensor_id, s.device_key, d.mac, d.ssid, d.crypt,
         s.med, s.mean, s.sd, 1.4826 * m.mad,
         round(s.p5)::smallint, round(s.p95)::smallint,
         s.main_freq, s.n_obs, s.n_days, s.w_start, s.w_end, now()
  FROM stats s
  JOIN mad m ON m.device_key = s.device_key
  JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
  ON CONFLICT (sensor_id, device_key) DO UPDATE SET
      bssid = EXCLUDED.bssid,
      ssid  = CASE WHEN ap_baselines.trusted THEN ap_baselines.ssid  ELSE EXCLUDED.ssid  END,
      crypt = CASE WHEN ap_baselines.trusted THEN ap_baselines.crypt ELSE EXCLUDED.crypt END,
      rssi_median    = EXCLUDED.rssi_median,
      rssi_mean      = EXCLUDED.rssi_mean,
      rssi_sd        = EXCLUDED.rssi_sd,
      rssi_robust_sd = EXCLUDED.rssi_robust_sd,
      rssi_p5        = EXCLUDED.rssi_p5,
      rssi_p95       = EXCLUDED.rssi_p95,
      main_freq_khz  = EXCLUDED.main_freq_khz,
      n_obs          = EXCLUDED.n_obs,
      n_days         = EXCLUDED.n_days,
      window_start   = EXCLUDED.window_start,
      window_end     = EXCLUDED.window_end,
      computed_at    = EXCLUDED.computed_at;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

-- AP inventory with the derived columns the dashboard (and later detection)
-- need, defined once: OUI, randomised (locally administered) BSSID, hidden
-- SSID, lifetime, plus the baseline if one exists.
CREATE OR REPLACE VIEW ap_inventory AS
SELECT d.sensor_id,
       d.device_key,
       d.mac                                   AS bssid,
       trunc(d.mac)                            AS oui,
       (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00' AS random_bssid,
       d.manuf,
       d.ssid,
       (d.ssid IS NULL OR d.ssid = '')         AS hidden,
       d.cloaked, d.crypt, d.crypt_bits, d.mfp_sup, d.mfp_req,
       d.adv_channel, d.ht_mode, d.beacon_rate, d.country,
       d.first_seen,
       d.last_seen,
       d.last_seen - d.first_seen              AS lifetime,
       d.config_changed_at,
       b.trusted,
       b.rssi_median, b.rssi_robust_sd, b.rssi_sd,
       b.n_obs                                 AS baseline_n_obs,
       b.n_days                                AS baseline_n_days,
       b.computed_at                           AS baseline_at
FROM devices d
LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
WHERE d.type = 'ap';

COMMIT;

-- --- later: TimescaleDB (NOT enabled; zero overhead for the seeded demo) --------------
-- The primary keys above already contain ts, which is what a hypertable's unique
-- index requires, so the conversion is a rewrite-free one-liner per table:
--
--   CREATE EXTENSION IF NOT EXISTS timescaledb;
--   SELECT create_hypertable('observations', 'ts',
--                            chunk_time_interval => INTERVAL '7 days', migrate_data => true);
--   SELECT create_hypertable('polls', 'ts',
--                            chunk_time_interval => INTERVAL '30 days', migrate_data => true);
--   -- then continuous aggregates: per-AP hourly RSSI median / n_obs, per-day AP activity.
