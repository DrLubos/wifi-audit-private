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
-- 5: covering index observations_device_ts_rssi_floor (rssi, rssi_floor)
--    replaces observations_device_ts_rssi; view ap_config_changes (the
--    cleaned configuration timeline), ap_channel_changes derived from it
-- 6: dashboard read tables ap_rssi_hourly, sensor_hourly, ap_summary,
--    sensor_summary, filled by refresh_rollups(); ap_config_changes is a
--    materialized view refreshed there (ap_channel_changes stays a view on it)
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')
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
-- It covers every column the AP queries read (rssi and, since schema 4,
-- rssi_floor through rssi_is_floor()), so /api/aps/{key} and its /rssi bucket
-- query run as index-only scans. Rows of one AP are spread over the whole heap
-- (inserted in time order, ~1 row per page), so a heap fetch costs one page per
-- reading. Measured 2026-09-28 on a busy AP (~29.7k readings): with
-- INCLUDE (rssi) only, the schema-4 queries fell back to an Index Scan reading
-- ~30,000 heap pages - 6.6-9.0 s cold and 3.5-4.2 s on repeat (235 MB > 128 MB
-- shared_buffers); as index-only scans ~400 index pages, 0.3-2.4 s cold and
-- 14-30 ms warm. Index-only scans need the heap pages all-visible: the seed
-- importer vacuums observations after each load.
-- Building it on the 1 GB box: SHARE lock on observations (reads continue,
-- writes such as a seed import wait), maintenance_work_mem-bounded sort,
-- peak extra disk ~ the new index plus its sort spill (~0.6 GB); the old
-- index is freed at COMMIT.
CREATE INDEX IF NOT EXISTS observations_device_ts_rssi_floor
  ON observations (sensor_id, device_key, ts) INCLUDE (rssi, rssi_floor);
-- Its predecessors (same leading columns, fewer INCLUDE columns) are redundant.
DROP INDEX IF EXISTS observations_device_ts_rssi;
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

-- Changes of an AP's advertised configuration, one row per changed field - the
-- one reading of the configuration timeline (docs/findings.md section 11).
-- device_config_history holds the REPLACED configuration at the poll ts where
-- the new one was first seen, devices the current one; neither is rewritten,
-- every column stays (a crypt/MFP/country/beacon-rate change is a security
-- signal). Rules, in the spirit of rssi_valid():
--   1. a state without a beacon record (crypt and adv_channel both NULL) says
--      nothing about any field and is skipped;
--   2. per field, NULL = unknown and is skipped: before collector buffer v5
--      (2026-09-27 19:57:29 UTC) an incomplete beacon record (channel, HT mode,
--      beacon rate and country missing) wrote a row on almost every poll,
--      A -> incomplete -> A;
--   3. crypt_bits 0 is Kismet's "field missing" unless the crypt string is
--      'Open' (an open AP's real bitfield is 0), so a WPA2 -> Open downgrade
--      still shows, in crypt and in crypt_bits;
--   4. a hidden AP's cloaked beacon (ssid '') carries no name, and its cloaked
--      flag is unknown there: hidden APs alternate between that record and a
--      named, uncloaked one, which is one state, not a change;
--   5. a change is a known value followed by a different known value, dated at
--      the ts where the old value was replaced.
-- Values are text (booleans 'true'/'false', crypt_bits decimal). Fixture test:
-- tests/sql/ap_config_changes_test.sql.
--
-- Schema 6: MATERIALIZED (the view read every history row of an AP on each
-- AP page: 85 ms warm for the busiest AP, 4.7 s for all APs). The definition
-- is unchanged; refresh_rollups() refreshes it after every import - history
-- only changes there. A schema-5 view of the same name is dropped first
-- (ap_channel_changes goes with it and is recreated below).
DO $$
BEGIN
  IF (SELECT relkind FROM pg_class WHERE oid = to_regclass('ap_config_changes')) = 'v' THEN
    DROP VIEW ap_config_changes CASCADE;
  END IF;
END $$;
CREATE MATERIALIZED VIEW IF NOT EXISTS ap_config_changes AS
WITH st AS (
  SELECT sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM device_config_history
  UNION ALL
  SELECT sensor_id, device_key, NULL::timestamptz, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM devices
), known AS (
  SELECT st.sensor_id, st.device_key, st.ts, f.field, f.val
  FROM st CROSS JOIN LATERAL (VALUES
    ('ssid',        nullif(st.ssid, '')),
    ('cloaked',     CASE WHEN st.ssid = '' THEN NULL ELSE st.cloaked::text END),
    ('crypt',       st.crypt),
    ('crypt_bits',  CASE WHEN st.crypt_bits = 0 AND st.crypt IS DISTINCT FROM 'Open' THEN NULL
                         ELSE st.crypt_bits::text END),
    ('mfp_sup',     st.mfp_sup::text),
    ('mfp_req',     st.mfp_req::text),
    ('adv_channel', st.adv_channel),
    ('ht_mode',     st.ht_mode),
    ('beacon_rate', st.beacon_rate::text),
    ('country',     st.country)) AS f(field, val)
  WHERE f.val IS NOT NULL AND NOT (st.crypt IS NULL AND st.adv_channel IS NULL)
), seq AS (
  SELECT k.sensor_id, k.device_key, k.ts, k.field, k.val,
         lead(k.val) OVER (PARTITION BY k.sensor_id, k.device_key, k.field
                           ORDER BY k.ts NULLS LAST) AS new_val
  FROM known k
)
SELECT sensor_id, device_key, ts, field, val AS old_value, new_val AS new_value
FROM seq
WHERE ts IS NOT NULL AND new_val IS NOT NULL AND new_val <> val
WITH DATA;
-- One row per (AP, ts, field): the unique index serves the per-AP lookups
-- (sensor_id, device_key, ts) and REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ap_config_changes_key
  ON ap_config_changes (sensor_id, device_key, ts, field);
COMMENT ON MATERIALIZED VIEW ap_config_changes IS
  'Changes of an AP''s advertised configuration, one row per changed field (old -> new), with the pre-v5 incomplete-record churn, Kismet''s missing-field crypt_bits 0 and the hidden-beacon/named-record alternation read as unknown. History is not rewritten. Materialized (schema 6), refreshed by refresh_rollups().';

-- Changes of an AP's advertised channel: the adv_channel rows of
-- ap_config_changes (same rows as its schema-4 definition: 11,388 on 138 APs,
-- compared 2026-09-28). Used by the evil_twin channel guard and fp-audit.
-- A plain view on the materialized one: as current as its last refresh.
CREATE OR REPLACE VIEW ap_channel_changes AS
SELECT sensor_id, device_key, ts, old_value AS from_channel, new_value AS to_channel
FROM ap_config_changes
WHERE field = 'adv_channel';

-- --- dashboard read tables (schema 6) ------------------------------------------------
--
-- Small tables the dashboard reads instead of the raw time series. Measured
-- before them (2026-09-29, tests/perf/measure.sh): /api/aps read 580 APs
-- scattered over 108k devices rows, the AP page recomputed the usual country
-- over all APs and read the AP's whole history, the overview ran two window
-- scans over every poll. Derived data only: written by refresh_rollups() and
-- nothing else, so together they are one snapshot of the source tables as of
-- sensor_summary.refreshed_at, from which the api derives its ETag. RSSI goes
-- through rssi_valid() / rssi_is_floor() as everywhere (floors are counted,
-- never a level). Hours are UTC hours (date_trunc('hour', ts, 'UTC')).

CREATE TABLE IF NOT EXISTS ap_rssi_hourly (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  hour       timestamptz NOT NULL,           -- start of the UTC hour
  n_obs      integer NOT NULL,               -- observation rows of the AP in the hour
  n_valid    integer NOT NULL,               -- readings with rssi_valid() not NULL
  n_floor    integer NOT NULL,               -- floor readings, rssi_is_floor() (censored)
  median     real,                           -- percentiles and extremes of the valid
  p10        real,                           --   readings only; NULL when n_valid = 0
  p90        real,
  min        smallint,
  max        smallint,
  PRIMARY KEY (sensor_id, device_key, hour),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
COMMENT ON TABLE ap_rssi_hourly IS
  'Per AP and UTC hour: observation count and the valid-RSSI distribution (floor readings counted in n_floor, never in the statistics). Filled by refresh_rollups().';

CREATE TABLE IF NOT EXISTS sensor_hourly (
  sensor_id       integer NOT NULL REFERENCES sensors(id),
  hour            timestamptz NOT NULL,      -- start of the UTC hour
  polls           integer NOT NULL,          -- collector polls in the hour
  polls_ok        integer NOT NULL,
  hop_ok_polls    integer NOT NULL,          -- polls with ds_hop_ok = true (full channel coverage)
  hop_known_polls integer NOT NULL,          -- polls with ds_hop_ok not NULL (buffer v4+)
  gap_s           real NOT NULL,             -- seconds of the hour inside a poll gap > 90 s
  active_aps      integer NOT NULL,          -- APs with an observation in the hour
  new_aps         integer NOT NULL,          -- APs first observed in the hour
  alerts          integer NOT NULL,          -- Kismet alerts with ts in the hour
  ap_obs          integer NOT NULL,          -- AP observation rows in the hour
  PRIMARY KEY (sensor_id, hour));
COMMENT ON TABLE sensor_hourly IS
  'Per sensor and UTC hour: poll health, hop coverage, gap time, AP activity and alerts. An hour inside a long gap has a row with polls = 0. Filled by refresh_rollups().';

CREATE TABLE IF NOT EXISTS ap_summary (
  sensor_id    integer NOT NULL,
  device_key   text NOT NULL,
  -- identity and advertised configuration: devices, as in ap_inventory
  bssid        macaddr NOT NULL,
  random_bssid boolean NOT NULL,             -- locally administered bit
  manuf        text,
  ssid         text,
  hidden       boolean NOT NULL,             -- no SSID in the current record
  cloaked      boolean,
  crypt        text,
  crypt_bits   bigint,
  mfp_sup      boolean,
  mfp_req      boolean,
  adv_channel  text,
  ht_mode      text,
  beacon_rate  integer,
  country      text,
  first_seen   timestamptz NOT NULL,
  last_seen    timestamptz NOT NULL,
  config_changed_at timestamptz,
  -- hidden APs (ap_config_changes rule 4): a cloaked beacon ('' + cloaked)
  -- seen now or in the history, and the current or latest name seen
  hidden_beacon boolean NOT NULL,
  name_seen     text,
  -- baseline: the ap_baselines row (has_baseline false = none; a row whose
  -- statistics refresh_ap_baselines() cleared has NULL statistics)
  has_baseline   boolean NOT NULL,
  trusted        boolean NOT NULL,
  trusted_at     timestamptz,
  note           text,
  rssi_median    real,
  rssi_mean      real,
  rssi_sd        real,
  rssi_robust_sd real,
  rssi_p5        smallint,
  rssi_p95       smallint,
  main_freq_khz  integer,
  baseline_n_obs   integer,
  baseline_n_floor integer,
  baseline_n_days  integer,
  baseline_window_start timestamptz,
  baseline_window_end   timestamptz,
  baseline_at    timestamptz,                -- ap_baselines.computed_at
  -- observations of the AP (sums of ap_rssi_hourly; first/last from the index)
  n_obs        bigint NOT NULL,
  n_valid      bigint NOT NULL,
  n_floor      bigint NOT NULL,
  first_obs    timestamptz,
  last_obs     timestamptz,
  -- configuration history
  history_rows   integer NOT NULL,           -- raw device_config_history rows (incl. the pre-v5 churn)
  n_changes      integer NOT NULL,           -- change events in ap_config_changes (distinct ts)
  last_change_at timestamptz,
  last_change    jsonb,                      -- [{field, old, new}] at last_change_at
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
COMMENT ON TABLE ap_summary IS
  'One row per AP with everything /api/aps and the AP page header show: inventory, baseline, observation counts, hidden-AP reading, config-change count. Rebuilt by refresh_rollups(); a change to ap_baselines (e.g. trusted) shows after the next refresh.';

CREATE TABLE IF NOT EXISTS sensor_summary (
  sensor_id        integer PRIMARY KEY REFERENCES sensors(id),
  first_poll       timestamptz,
  last_poll        timestamptz,
  polls            integer NOT NULL,
  polls_ok         integer NOT NULL,
  gap_count        integer NOT NULL,         -- poll gaps > 90 s
  gap_total_s      double precision NOT NULL,
  gap_max_s        double precision NOT NULL,
  gaps             jsonb NOT NULL,           -- the 10 longest: [{gap_start, gap_end, gap_s}]
  devices_by_type  jsonb NOT NULL,           -- {"ap": n, "client": n, ...}
  obs_total        bigint NOT NULL,          -- sum(polls.new_obs): rows the collector wrote, all device types
  obs_ap           bigint NOT NULL,          -- AP observation rows on the server
  country_expected text,                     -- the country most of the sensor's APs advertise
  alerts           integer NOT NULL,
  alerts_last_ts   timestamptz,
  baselines        integer NOT NULL,         -- ap_baselines rows with statistics
  refreshed_at     timestamptz NOT NULL);    -- when refresh_rollups() last ran: the api's ETag
COMMENT ON TABLE sensor_summary IS
  'One row per sensor: what the overview shows, plus country_expected for the AP page. refreshed_at versions every read table of the sensor.';

-- Recompute the read tables of one sensor from the hour of p_from onward
-- (p_from NULL, or the sensor never refreshed: the whole history). Idempotent;
-- the seed importer calls it after every load with the earliest ts it
-- inserted. Per hour tables: the hours >= the hour of p_from - moved back to
-- the hour of the last poll before p_from, so a poll gap ending after p_from
-- is split over all its hours again - are deleted and recomputed; an AP with
-- no hourly row left (new, or re-classified as an AP) is computed over its
-- whole history, rows of devices that are no longer APs are dropped, and the
-- sensor hours go back to the earliest hour either touched. Invariant: an
-- incremental run leaves the same rows as a full one (fixture test
-- tests/sql/refresh_rollups_test.sql). Then ap_config_changes is refreshed
-- (all sensors) and ap_summary / sensor_summary are rebuilt. Measured on the 12-day seed (2026-09-29, read-only): the
-- full-history AP hour aggregate took 16 s cold, the config view 4.7 s.
CREATE OR REPLACE FUNCTION refresh_rollups(p_sensor_id integer, p_from timestamptz DEFAULT NULL)
RETURNS TABLE (from_hour timestamptz, ap_hours integer, sensor_hours integer, aps integer,
               refreshed_at timestamptz)
LANGUAGE plpgsql
SET work_mem = '32MB'      -- the hour aggregate sorts ~1.8M rows on a full run
SET jit = off              -- JIT compilation costs more than it saves on these statements
AS $$
#variable_conflict use_column
DECLARE
  v_from timestamptz;        -- first hour recomputed for every AP
  v_sfrom timestamptz;       -- first hour recomputed per sensor (earlier after a re-classification)
  v_gone timestamptz;        -- first hour of rows dropped for devices that are no longer APs
  v_new  timestamptz;        -- first hour of APs computed over their whole history
  v_now  timestamptz := clock_timestamp();
  v_ap_hours integer;
  v_sensor_hours integer;
  v_aps integer;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE id = p_sensor_id) THEN
    RAISE EXCEPTION 'refresh_rollups: unknown sensor_id %', p_sensor_id;
  END IF;
  -- one refresh per sensor at a time: two would delete and insert the same rows
  PERFORM pg_advisory_xact_lock(hashtext('refresh_rollups'), p_sensor_id);

  -- 1. first hour to recompute
  IF p_from IS NULL OR NOT EXISTS (SELECT 1 FROM sensor_summary WHERE sensor_id = p_sensor_id) THEN
    v_from := '-infinity';
  ELSE
    v_from := date_trunc('hour',
                least(p_from, (SELECT max(ts) FROM polls WHERE sensor_id = p_sensor_id AND ts < p_from)),
                'UTC');
  END IF;

  -- 2. per AP and hour
  WITH gone AS (
    DELETE FROM ap_rssi_hourly h
    WHERE h.sensor_id = p_sensor_id
      AND NOT EXISTS (SELECT 1 FROM devices d
                      WHERE d.sensor_id = h.sensor_id AND d.device_key = h.device_key
                        AND d.type = 'ap')
    RETURNING h.hour)
  SELECT min(hour) INTO v_gone FROM gone;
  DELETE FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND hour >= v_from;
  SELECT min(date_trunc('hour', (SELECT min(o.ts) FROM observations o
                                 WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
                        'UTC'))
    INTO v_new
  FROM devices d
  WHERE d.sensor_id = p_sensor_id AND d.type = 'ap'
    AND NOT EXISTS (SELECT 1 FROM ap_rssi_hourly h
                    WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key);
  v_sfrom := least(v_from, v_gone, v_new);
  INSERT INTO ap_rssi_hourly (sensor_id, device_key, hour, n_obs, n_valid, n_floor,
                              median, p10, p90, min, max)
  SELECT p_sensor_id, device_key, hour, n_obs, n_valid, n_floor, pct[1], pct[2], pct[3], lo, hi
  FROM (
    SELECT a.device_key, date_trunc('hour', o.ts, 'UTC') AS hour,
           count(*)::integer AS n_obs,
           count(rssi_valid(o.rssi))::integer AS n_valid,
           (count(*) FILTER (WHERE rssi_is_floor(o.rssi, o.rssi_floor)))::integer AS n_floor,
           percentile_cont(ARRAY[0.5, 0.1, 0.9]) WITHIN GROUP (ORDER BY rssi_valid(o.rssi)) AS pct,
           min(rssi_valid(o.rssi)) AS lo,
           max(rssi_valid(o.rssi)) AS hi
    FROM (SELECT d.device_key,
                 CASE WHEN EXISTS (SELECT 1 FROM ap_rssi_hourly h
                                   WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key)
                      THEN v_from ELSE '-infinity'::timestamptz END AS since
          FROM devices d
          WHERE d.sensor_id = p_sensor_id AND d.type = 'ap') a
    JOIN observations o
      ON o.sensor_id = p_sensor_id AND o.device_key = a.device_key AND o.ts >= a.since
    GROUP BY 1, 2) s;
  GET DIAGNOSTICS v_ap_hours = ROW_COUNT;

  -- 3. per sensor and hour (from v_sfrom: a re-classified device changed
  --    the AP counts of its hours before v_from too)
  DELETE FROM sensor_hourly WHERE sensor_id = p_sensor_id AND hour >= v_sfrom;
  INSERT INTO sensor_hourly (sensor_id, hour, polls, polls_ok, hop_ok_polls, hop_known_polls,
                             gap_s, active_aps, new_aps, alerts, ap_obs)
  WITH p AS (
    SELECT date_trunc('hour', ts, 'UTC') AS hour, count(*) AS polls,
           count(*) FILTER (WHERE ok) AS polls_ok,
           count(*) FILTER (WHERE ds_hop_ok) AS hop_ok, count(ds_hop_ok) AS hop_known
    FROM polls WHERE sensor_id = p_sensor_id AND ts >= v_sfrom
    GROUP BY 1
  ), g AS (    -- poll gaps > 90 s as on the overview, incl. one ending at the first poll >= v_sfrom
    SELECT prev_ts, ts
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts
          FROM polls
          WHERE sensor_id = p_sensor_id
            AND ts >= coalesce((SELECT max(ts) FROM polls
                                WHERE sensor_id = p_sensor_id AND ts < v_sfrom), v_sfrom)) x
    WHERE ts - prev_ts > interval '90 seconds'
  ), gh AS (   -- ... split over the hours they cover
    SELECT h.hour,
           sum(extract(epoch FROM least(g.ts, h.hour + interval '1 hour') - greatest(g.prev_ts, h.hour))) AS gap_s
    FROM g
    CROSS JOIN LATERAL generate_series(date_trunc('hour', g.prev_ts, 'UTC'), g.ts,
                                       interval '1 hour') AS h(hour)
    WHERE h.hour >= v_sfrom AND h.hour < g.ts
    GROUP BY h.hour
  ), a AS (
    SELECT hour, count(*) AS active_aps, sum(n_obs) AS ap_obs
    FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND hour >= v_sfrom
    GROUP BY hour
  ), f AS (    -- the first hour in which each AP was observed
    SELECT first_hour AS hour, count(*) AS new_aps
    FROM (SELECT min(hour) AS first_hour FROM ap_rssi_hourly
          WHERE sensor_id = p_sensor_id GROUP BY device_key) x
    WHERE first_hour >= v_sfrom
    GROUP BY first_hour
  ), al AS (
    SELECT date_trunc('hour', ts, 'UTC') AS hour, count(*) AS alerts
    FROM alerts WHERE sensor_id = p_sensor_id AND ts >= v_sfrom
    GROUP BY 1
  ), hrs AS (
    SELECT hour FROM p UNION SELECT hour FROM gh UNION SELECT hour FROM a UNION SELECT hour FROM al
  )
  SELECT p_sensor_id, hrs.hour, coalesce(p.polls, 0), coalesce(p.polls_ok, 0),
         coalesce(p.hop_ok, 0), coalesce(p.hop_known, 0), coalesce(gh.gap_s, 0),
         coalesce(a.active_aps, 0), coalesce(f.new_aps, 0), coalesce(al.alerts, 0),
         coalesce(a.ap_obs, 0)
  FROM hrs
  LEFT JOIN p  ON p.hour  = hrs.hour
  LEFT JOIN gh ON gh.hour = hrs.hour
  LEFT JOIN a  ON a.hour  = hrs.hour
  LEFT JOIN f  ON f.hour  = hrs.hour
  LEFT JOIN al ON al.hour = hrs.hour;
  GET DIAGNOSTICS v_sensor_hours = ROW_COUNT;

  -- 4. the configuration timeline (all sensors; history only changes with an
  --    import). CONCURRENTLY: AP pages and detectors keep reading meanwhile.
  IF (SELECT relispopulated FROM pg_class WHERE oid = to_regclass('ap_config_changes')) THEN
    REFRESH MATERIALIZED VIEW CONCURRENTLY ap_config_changes;
  ELSE
    REFRESH MATERIALIZED VIEW ap_config_changes;
  END IF;

  -- 5. one row per AP, rebuilt
  DELETE FROM ap_summary WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_summary (
      sensor_id, device_key, bssid, random_bssid, manuf, ssid, hidden, cloaked, crypt, crypt_bits,
      mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country, first_seen, last_seen,
      config_changed_at, hidden_beacon, name_seen,
      has_baseline, trusted, trusted_at, note, rssi_median, rssi_mean, rssi_sd, rssi_robust_sd,
      rssi_p5, rssi_p95, main_freq_khz, baseline_n_obs, baseline_n_floor, baseline_n_days,
      baseline_window_start, baseline_window_end, baseline_at,
      n_obs, n_valid, n_floor, first_obs, last_obs,
      history_rows, n_changes, last_change_at, last_change)
  SELECT d.sensor_id, d.device_key, d.mac,
         (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00',
         d.manuf, d.ssid, (d.ssid IS NULL OR d.ssid = ''), d.cloaked, d.crypt, d.crypt_bits,
         d.mfp_sup, d.mfp_req, d.adv_channel, d.ht_mode, d.beacon_rate, d.country,
         d.first_seen, d.last_seen, d.config_changed_at,
         coalesce(hi.hidden_beacon, false) OR (coalesce(d.cloaked, false) AND coalesce(d.ssid, '') = ''),
         coalesce(nullif(d.ssid, ''), hi.name_seen),
         b.device_key IS NOT NULL, coalesce(b.trusted, false), b.trusted_at, b.note,
         b.rssi_median, b.rssi_mean, b.rssi_sd, b.rssi_robust_sd, b.rssi_p5, b.rssi_p95,
         b.main_freq_khz, b.n_obs, b.n_floor, b.n_days, b.window_start, b.window_end, b.computed_at,
         coalesce(r.n_obs, 0), coalesce(r.n_valid, 0), coalesce(r.n_floor, 0),
         (SELECT min(o.ts) FROM observations o
          WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
         (SELECT max(o.ts) FROM observations o
          WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
         coalesce(hi.n_rows, 0), coalesce(c.n_changes, 0), c.last_change_at, c.last_change
  FROM devices d
  LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
  LEFT JOIN (SELECT device_key, sum(n_obs) AS n_obs, sum(n_valid) AS n_valid, sum(n_floor) AS n_floor
             FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id
             GROUP BY device_key) r ON r.device_key = d.device_key
  LEFT JOIN (SELECT device_key, count(*) AS n_rows,
                    bool_or(cloaked AND ssid = '') AS hidden_beacon,
                    (array_agg(ssid ORDER BY ts DESC) FILTER (WHERE ssid <> ''))[1] AS name_seen
             FROM device_config_history WHERE sensor_id = p_sensor_id
             GROUP BY device_key) hi ON hi.device_key = d.device_key
  LEFT JOIN (SELECT device_key, count(*) AS n_changes, max(ts) AS last_change_at,
                    (array_agg(changes ORDER BY ts DESC))[1] AS last_change
             FROM (SELECT device_key, ts,
                          jsonb_agg(jsonb_build_object('field', field, 'old', old_value,
                                                       'new', new_value) ORDER BY field) AS changes
                   FROM ap_config_changes WHERE sensor_id = p_sensor_id
                   GROUP BY device_key, ts) e
             GROUP BY device_key) c ON c.device_key = d.device_key
  WHERE d.sensor_id = p_sensor_id AND d.type = 'ap';
  GET DIAGNOSTICS v_aps = ROW_COUNT;

  -- 6. one row per sensor
  WITH gap AS (
    SELECT prev_ts AS gap_start, ts AS gap_end, ts - prev_ts AS len
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts
          FROM polls WHERE sensor_id = p_sensor_id) x
    WHERE ts - prev_ts > interval '90 seconds'
  )
  INSERT INTO sensor_summary (sensor_id, first_poll, last_poll, polls, polls_ok,
                              gap_count, gap_total_s, gap_max_s, gaps, devices_by_type,
                              obs_total, obs_ap, country_expected, alerts, alerts_last_ts,
                              baselines, refreshed_at)
  SELECT p_sensor_id, pl.first_poll, pl.last_poll, pl.polls, pl.polls_ok,
         (SELECT count(*) FROM gap),
         (SELECT coalesce(extract(epoch FROM sum(len)), 0) FROM gap),
         (SELECT coalesce(extract(epoch FROM max(len)), 0) FROM gap),
         (SELECT coalesce(jsonb_agg(jsonb_build_object('gap_start', gap_start, 'gap_end', gap_end,
                                                       'gap_s', extract(epoch FROM len)::float8)
                                    ORDER BY len DESC), '[]'::jsonb)
          FROM (SELECT * FROM gap ORDER BY len DESC LIMIT 10) t),
         (SELECT coalesce(jsonb_object_agg(type, n), '{}'::jsonb)
          FROM (SELECT type, count(*) AS n FROM devices WHERE sensor_id = p_sensor_id
                GROUP BY type) t),
         pl.obs_total,
         (SELECT coalesce(sum(n_obs), 0) FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id),
         (SELECT mode() WITHIN GROUP (ORDER BY country) FROM devices
          WHERE sensor_id = p_sensor_id AND type = 'ap' AND country IS NOT NULL),
         (SELECT count(*) FROM alerts WHERE sensor_id = p_sensor_id),
         (SELECT max(ts) FROM alerts WHERE sensor_id = p_sensor_id),
         (SELECT count(*) FROM ap_baselines WHERE sensor_id = p_sensor_id AND rssi_median IS NOT NULL),
         v_now
  FROM (SELECT min(ts) AS first_poll, max(ts) AS last_poll, count(*) AS polls,
               count(*) FILTER (WHERE ok) AS polls_ok, coalesce(sum(new_obs), 0) AS obs_total
        FROM polls WHERE sensor_id = p_sensor_id) pl
  ON CONFLICT (sensor_id) DO UPDATE SET
      first_poll = EXCLUDED.first_poll, last_poll = EXCLUDED.last_poll,
      polls = EXCLUDED.polls, polls_ok = EXCLUDED.polls_ok,
      gap_count = EXCLUDED.gap_count, gap_total_s = EXCLUDED.gap_total_s,
      gap_max_s = EXCLUDED.gap_max_s, gaps = EXCLUDED.gaps,
      devices_by_type = EXCLUDED.devices_by_type, obs_total = EXCLUDED.obs_total,
      obs_ap = EXCLUDED.obs_ap, country_expected = EXCLUDED.country_expected,
      alerts = EXCLUDED.alerts, alerts_last_ts = EXCLUDED.alerts_last_ts,
      baselines = EXCLUDED.baselines, refreshed_at = EXCLUDED.refreshed_at;

  RETURN QUERY SELECT v_sfrom, v_ap_hours, v_sensor_hours, v_aps, v_now;
END
$$;

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
