-- wifi-audit server schema (PostgreSQL 14+, no extensions).
--
-- Idempotent: run as often as you like with
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
--
-- Mirrors the sensor's SQLite buffer (wifi-sensor/collector/store.py, schema v3)
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
-- 2: observations.disconnects_last (buffer v3)
-- 3: polls.ds_hop_n / ds_hop_visited / ds_hop_ok (buffer v4)
-- 4: RSSI floors as censored values: rssi_valid(), rssi_is_floor(),
--    observations.rssi_floor (buffer v5), ap_baselines.n_floor; view
--    ap_channel_changes; refresh_ap_baselines() clears baselines that no
--    longer qualify
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '4')
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
  ds_hop_n       integer,                    -- live hop-list length (buffer v4)
  ds_hop_visited integer,                    -- hop entries really tuned (buffer v4)
  ds_hop_ok      boolean,                    -- configured list, fully visited (buffer v4; NULL = no explicit list / older buffer)
  duration_ms    integer,
  ok             boolean NOT NULL DEFAULT true,
  error          text,
  PRIMARY KEY (sensor_id, ts));
-- Schema 3: hop-list coverage on a polls table created by an earlier schema
-- (metadata only; rows from older buffers keep NULL). Degraded channel coverage
-- (Kismet 2025-09 collapsing the hop list) is invisible in ds_packets - see
-- wifi-sensor/docs/findings.md section 7.
ALTER TABLE polls ADD COLUMN IF NOT EXISTS ds_hop_n integer;
ALTER TABLE polls ADD COLUMN IF NOT EXISTS ds_hop_visited integer;
ALTER TABLE polls ADD COLUMN IF NOT EXISTS ds_hop_ok boolean;
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
  freq_khz   integer,                        -- Kismet device frequency: NOT the frame behind rssi (see below)
  channel    text,                           -- Kismet last known channel
  rssi       smallint,                       -- Kismet sig_last (one frame), NULL when no reading;
                                             -- may hold a raw floor value (read via rssi_valid())
  rssi_min   smallint,                       -- Kismet lifetime extremes
  rssi_max   smallint,
  pk_total   bigint,                         -- cumulative counters as Kismet reports them
  pk_tx      bigint,
  pk_rx      bigint,
  pk_data    bigint,
  bytes      bigint,
  -- AP only (NULL otherwise)
  n_clients     integer,
  disconnects   integer,                     -- client_disconnects: size of the current deauth/disassoc burst
  disconnects_last timestamptz,              -- last deauth/disassoc frame (buffer v3; NULL before, or none yet)
  qbss_stations integer,                     -- AP-reported station count
  util_pct      real,                        -- AP-reported channel utilisation
  bss_timestamp bigint,                      -- AP uptime (us); a drop means a restart
  ie_checksum   bigint,                      -- beacon IE checksum (varies per beacon)
  beacon_fp     bigint,                      -- beacon fingerprint
  -- client only
  bssid      macaddr,
  PRIMARY KEY (ts, sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
-- Schema 2: the column above on a table created by schema 1 (metadata only, rows
-- seeded from a v2 buffer keep NULL and the detectors fall back to `disconnects`).
ALTER TABLE observations ADD COLUMN IF NOT EXISTS disconnects_last timestamptz;
-- Schema 4: buffer v5 stores the adapter's floor readings (-106/-120) as
-- rssi NULL + rssi_floor true; rows imported from older buffers keep the raw
-- value in rssi and NULL here. Never rewritten - read through rssi_valid() /
-- rssi_is_floor().
ALTER TABLE observations ADD COLUMN IF NOT EXISTS rssi_floor boolean;
COMMENT ON COLUMN observations.freq_khz IS
  'Kismet kismet.device.base.frequency: frequency of the frame that last updated the device frequency (Kismet prefers the frame''s own channel info, else the tuned channel, and updates it from other frames than the signal). Not the reception channel of rssi and not reliably the AP''s channel - use devices.adv_channel.';
-- The per-device time-series index (the PK is time-leading for the hypertable).
-- It covers rssi so the bucket query of /api/aps/{key}/rssi runs as an
-- index-only scan: without INCLUDE (rssi) it touched ~14k heap pages per AP
-- (rows of one AP are spread over the whole table, inserted in time order),
-- 3-8 s on a cold cache; with it, ~125 index pages and ~0.1 s.
CREATE INDEX IF NOT EXISTS observations_device_ts_rssi
  ON observations (sensor_id, device_key, ts) INCLUDE (rssi);
-- Its predecessor without INCLUDE (same leading columns) is redundant.
DROP INDEX IF EXISTS observations_device_ts;
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
  n_obs          integer,                    -- valid RSSI readings (floor values excluded)
  n_floor        integer,                    -- floor readings (censored) in the same window (schema 4)
  n_days         integer,                    -- distinct days (in sensors.tz) with a reading
  window_start   timestamptz,
  window_end     timestamptz,
  computed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
-- Schema 4: censored floor readings counted next to the valid ones.
ALTER TABLE ap_baselines ADD COLUMN IF NOT EXISTS n_floor integer;
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
-- One row per (detector, subject, episode start). The batch detectors in
-- detection/ refresh the row of an episode they have already stored
-- (detection/detections.py matches on the stored window) instead of adding one;
-- this index is the guarantee for an exact re-run. The subject is the device_key
-- or, for a BSSID without a device row, the upper-case MAC.
CREATE UNIQUE INDEX IF NOT EXISTS detections_dedupe
  ON detections (sensor_id, type, coalesce(device_key, upper(mac::text)), ts);
COMMENT ON TABLE detections IS
  'Server-side detections, written only by the batch detectors in detection/ (deauth_flood so far); the dashboard reads and acknowledges them. Re-runs refresh a stored episode, acked/ack_note survive.';

-- --- helpers ------------------------------------------------------------------------------

-- RSSI floors of the capture adapter (RTL8821CU / rtw88_8821cu): -106 dBm is the
-- smallest value its CCK power estimate can produce (lna_gain_table_1: -44 - 2*31;
-- every 2.4 GHz beacon is CCK) and -120 the OFDM clamp (max(PWDB - 110, -120)).
-- A floor means "at or below the floor", i.e. a censored reading, never a level:
-- statistics use rssi_valid(), and the share of floors per AP (n_floor) gates
-- eligibility. Raw rows are never rewritten. The same list is
-- wifi-sensor/collector/dataset_rules.py RSSI_FLOOR_DBM (a collector test keeps
-- them equal); a different capture adapter needs its floors re-derived.
CREATE OR REPLACE FUNCTION rssi_valid(r smallint) RETURNS smallint
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN r IN (-106, -120) THEN NULL ELSE r END
$$;

-- A floor reading: a raw floor value (rows from buffers before v5) or the v5 flag.
CREATE OR REPLACE FUNCTION rssi_is_floor(r smallint, flag boolean) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(flag, false) OR coalesce(r IN (-106, -120), false)
$$;

-- Recompute the RSSI baselines of one sensor from its observations.
-- Defaults match analysis/rssi_stability.py: an AP qualifies with >= 200
-- valid readings on >= 2 distinct days. Floor readings are excluded from every
-- statistic and counted in n_floor (floor share = n_floor / (n_obs + n_floor));
-- baselines of APs that no longer qualify are cleared (statistics NULL). p_from / p_until restrict the readings used
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
    SELECT o.device_key, o.ts, rssi_valid(o.rssi) AS rssi, o.freq_khz,
           rssi_is_floor(o.rssi, o.rssi_floor) AS is_floor,
           (o.ts AT TIME ZONE v_tz)::date AS obs_day
    FROM observations o
    JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
    WHERE o.sensor_id = p_sensor_id
      AND d.type = 'ap'
      AND (o.rssi IS NOT NULL OR o.rssi_floor)
      AND (p_from  IS NULL OR o.ts >= p_from)
      AND (p_until IS NULL OR o.ts <  p_until)
  ), stats AS (
    -- statistics over valid readings only; floors are counted, never averaged
    SELECT device_key,
           count(rssi)::integer                                  AS n_obs,
           count(*) FILTER (WHERE is_floor)::integer             AS n_floor,
           count(DISTINCT obs_day) FILTER (WHERE rssi IS NOT NULL)::integer AS n_days,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY rssi)    AS med,
           avg(rssi)                                             AS mean,
           stddev_pop(rssi)                                      AS sd,
           percentile_cont(0.05) WITHIN GROUP (ORDER BY rssi)    AS p5,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY rssi)    AS p95,
           mode() WITHIN GROUP (ORDER BY freq_khz) FILTER (WHERE rssi IS NOT NULL) AS main_freq,
           min(ts) FILTER (WHERE rssi IS NOT NULL)               AS w_start,
           max(ts) FILTER (WHERE rssi IS NOT NULL)               AS w_end
    FROM obs
    GROUP BY device_key
    HAVING count(rssi) >= p_min_obs
       AND count(DISTINCT obs_day) FILTER (WHERE rssi IS NOT NULL) >= p_min_days
  ), mad AS (
    SELECT o.device_key,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(o.rssi - s.med)) AS mad
    FROM obs o
    JOIN stats s ON s.device_key = o.device_key
    WHERE o.rssi IS NOT NULL
    GROUP BY o.device_key
  )
  INSERT INTO ap_baselines (
      sensor_id, device_key, bssid, ssid, crypt,
      rssi_median, rssi_mean, rssi_sd, rssi_robust_sd, rssi_p5, rssi_p95,
      main_freq_khz, n_obs, n_floor, n_days, window_start, window_end, computed_at)
  SELECT p_sensor_id, s.device_key, d.mac, d.ssid, d.crypt,
         s.med, s.mean, s.sd, 1.4826 * m.mad,
         round(s.p5)::smallint, round(s.p95)::smallint,
         s.main_freq, s.n_obs, s.n_floor, s.n_days, s.w_start, s.w_end, now()
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
      n_floor        = EXCLUDED.n_floor,
      n_days         = EXCLUDED.n_days,
      window_start   = EXCLUDED.window_start,
      window_end     = EXCLUDED.window_end,
      computed_at    = EXCLUDED.computed_at;
  GET DIAGNOSTICS v_n = ROW_COUNT;

  -- Schema 4: a baseline row this call did not refresh (the AP no longer
  -- qualifies on valid readings in the window) loses its statistics, so no
  -- consumer keeps using a stale or floor-dominated baseline. The row itself,
  -- with trusted/note, stays.
  UPDATE ap_baselines SET
      rssi_median = NULL, rssi_mean = NULL, rssi_sd = NULL, rssi_robust_sd = NULL,
      rssi_p5 = NULL, rssi_p95 = NULL, main_freq_khz = NULL,
      n_obs = NULL, n_floor = NULL, n_days = NULL, window_start = NULL, window_end = NULL,
      computed_at = now()
  WHERE sensor_id = p_sensor_id AND computed_at < now();

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
       b.computed_at                           AS baseline_at,
       b.n_floor                               AS baseline_n_floor,
       b.n_floor::real / nullif(b.n_obs + b.n_floor, 0) AS baseline_floor_share
FROM devices d
LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
WHERE d.type = 'ap';

-- Changes of an AP's advertised channel. device_config_history holds the
-- REPLACED configuration at the poll ts where the new one was first seen.
-- Before collector buffer v5 an empty beacon record (channel/HT/beacon rate
-- unknown) also wrote a row on almost every poll; unknown values are therefore
-- skipped and a change is a known channel followed by a different known one
-- (the next known replaced value, else the device's current channel).
CREATE OR REPLACE VIEW ap_channel_changes AS
WITH k AS (
  SELECT h.sensor_id, h.device_key, h.ts, h.adv_channel AS before_ch
  FROM device_config_history h
  WHERE h.adv_channel IS NOT NULL
), seq AS (
  SELECT k.sensor_id, k.device_key, k.ts, k.before_ch,
         coalesce(lead(k.before_ch) OVER (PARTITION BY k.sensor_id, k.device_key ORDER BY k.ts),
                  d.adv_channel) AS after_ch
  FROM k
  JOIN devices d ON d.sensor_id = k.sensor_id AND d.device_key = k.device_key
)
SELECT sensor_id, device_key, ts, before_ch AS from_channel, after_ch AS to_channel
FROM seq
WHERE after_ch IS NOT NULL AND after_ch <> before_ch;

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
