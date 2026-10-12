-- Sensor history of pi-fri for the dashboard (schema 9): the documented
-- outages, coverage losses and changes (wifi-sensor/docs/findings.md sections
-- 1, 6 and 7; detection/degraded_windows.csv) as sensor_annotations, and the
-- capture-configuration history as sensor_config versions (confirmed_at = the
-- effective time). Times UTC. Idempotent (ON CONFLICT DO NOTHING): rows the
-- developer has edited since are kept. Run by ops/migrate_schema9.sh; by hand:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/seed_sensor_history.sql
--
-- Three windows below were found in the server's polls on 2026-10-11 by the
-- sensor_periods derivation and are not investigated: 2026-09-17 19:38:27 -
-- 21:53:20 and 2026-09-21 22:08:19 - 22:13:49 (polls ok, no devices),
-- 2026-10-06 12:23:51 - 13:24:45 (no polls). A new findings row goes into
-- findings.md section 6 and degraded_windows.csv with the same times.
--
-- Corrections of 2026-10-12 (the developer's decisions after the Sensors
-- page): 2026-09-27 12:17:20 - 12:22:55 is a no-frames window (findings
-- section 6: the re-opened source captured nothing), not limited coverage,
-- so the outage the polls show there gets its cause; the first hour after
-- installation (2026-09-15) is documented as not investigated; the 2026-10-06
-- note gives its time in local time like the tables. The UPDATEs below apply
-- them to rows still as first seeded (an edited row is left alone) and run
-- before the INSERT, which then skips what exists.
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE name = 'pi-fri') THEN
    RAISE EXCEPTION 'seed_sensor_history: no sensor pi-fri';
  END IF;
END $$;

UPDATE sensor_annotations a
SET kind = 'no_frames', label = 'Capture-helper crashes at 12:17:20/39/47; the re-opened source captured nothing',
    ref = 'findings.md section 6'
WHERE a.sensor_id = (SELECT id FROM sensors WHERE name = 'pi-fri')
  AND a.starts_at = '2026-09-27 12:17:20+00' AND a.kind = 'coverage'
  AND a.label = 'Channel 1 only / no frames after capture-helper crashes';
UPDATE sensor_annotations a
SET label = 'No polls for 1 h; the collector restarted at 15:24:44 local time (not investigated)'
WHERE a.sensor_id = (SELECT id FROM sensors WHERE name = 'pi-fri')
  AND a.starts_at = '2026-10-06 12:23:51+00' AND a.kind = 'gap'
  AND a.label = 'No polls for 1 h, collector restarted 13:24:44 (not investigated)';

INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, boundary, label, ref)
SELECT (SELECT id FROM sensors WHERE name = 'pi-fri'), v.starts_at::timestamptz, v.ends_at::timestamptz,
       v.kind, v.boundary, v.label, v.ref
FROM (VALUES
  ('2026-09-15 13:53:07+00', NULL, 'change', true, 'Collector started: first poll (start of the measurements)', 'findings.md section 1'),
  ('2026-09-15 13:53:07+00', '2026-09-15 14:55:17+00', 'gap', false, 'First hour after installation (not investigated)', 'findings.md section 1'),
  ('2026-09-17 19:36:17+00', '2026-09-17 19:38:27+00', 'gap', false, 'Short gap (not investigated)', 'findings.md section 6'),
  ('2026-09-17 19:38:27+00', '2026-09-17 21:53:20+00', 'no_frames', false, 'Polls ok but no devices (not investigated)', 'findings.md section 6'),
  ('2026-09-17 21:37:57+00', '2026-09-17 21:49:50+00', 'gap', false, 'Short gap (not investigated)', 'findings.md section 6'),
  ('2026-09-19 12:18:01+00', '2026-09-19 12:25:01+00', 'gap', false, 'Short gap (not investigated)', 'findings.md section 6'),
  ('2026-09-21 16:26:28+00', '2026-09-21 21:47:58+00', 'gap', false, 'Kismet hung after a heap abort; stop blocked (TimeoutStopSec=infinity)', 'findings.md section 6'),
  ('2026-09-21 22:08:19+00', '2026-09-21 22:13:49+00', 'no_frames', false, 'Polls ok but no devices (not investigated)', 'findings.md section 6'),
  ('2026-09-22 13:43:21+00', '2026-09-23 09:26:58+00', 'gap', false, 'Root filesystem full (Kismet logs)', 'findings.md section 6'),
  ('2026-09-26 22:45:26+00', '2026-09-27 13:04:57+00', 'coverage', false, '45 of 90 hop entries visited (stride 4 vs 90)', 'findings.md section 7'),
  ('2026-09-26 22:45:26+00', NULL, 'change', true, 'Hop list: explicit 90-entry list (Kismet''s 91 without 165HT40-)', 'findings.md section 7'),
  ('2026-09-26 22:46:43+00', '2026-09-26 22:48:23+00', 'gap', false, 'Controlled reboot after the hop-list and journal install', 'findings.md section 6'),
  ('2026-09-26 23:09:14+00', '2026-09-27 11:34:24+00', 'coverage', false, 'Channel 1 only after a capture-helper crash', 'findings.md section 7'),
  ('2026-09-27 11:30:14+00', '2026-09-27 11:34:25+00', 'no_frames', false, 'Capture-helper crash, broken re-open captured nothing', 'findings.md section 6'),
  ('2026-09-27 12:17:20+00', '2026-09-27 12:22:55+00', 'no_frames', false, 'Capture-helper crashes at 12:17:20/39/47; the re-opened source captured nothing', 'findings.md section 6'),
  ('2026-09-27 13:04:57+00', NULL, 'change', true, 'Hop list: 89 entries (prime length, without 165HT40- and 140HT40-); web UI on loopback only', 'findings.md section 7'),
  ('2026-09-27 14:15:50+00', NULL, 'change', false, 'Hop guard: a collapsed hop list is restored within ~10 s', 'findings.md section 7'),
  ('2026-09-27 19:57:29+00', NULL, 'change', false, 'Collector buffer v5: RSSI floors stored as NULL, configuration merged field by field', 'findings.md section 7'),
  ('2026-10-03 11:00:20+00', NULL, 'change', false, 'Collector buffer v6: slim observations, AP load only as the latest value', 'findings.md section 7'),
  ('2026-10-06 12:23:51+00', '2026-10-06 13:24:45+00', 'gap', false, 'No polls for 1 h; the collector restarted at 15:24:44 local time (not investigated)', 'findings.md section 6'),
  ('2026-10-06 19:33:15+00', '2026-10-10 21:53:47+00', 'no_frames', false, 'Capture adapter lost from the USB bus', 'findings.md section 6'),
  ('2026-10-11 08:27:04+00', NULL, 'change', false, 'Capture watchdog power-cycles the adapter''s hub port', 'findings.md section 7'),
  ('2026-10-11 10:16:11+00', NULL, 'change', false, 'Collector buffer v7: hourly AP load recorded again', 'findings.md section 7')
) AS v(starts_at, ends_at, kind, boundary, label, ref)
ON CONFLICT DO NOTHING;

-- Capture configuration history: Kismet's autodetected list, the 90-entry
-- list (only 45 entries visited - stride 4), the 89-entry list
-- (wifi-sensor/install_sensor.sh RTW88_8821CU_CHANNELS). Dwell 0.2 s (5 hops/s),
-- poll 30 s. The orders of the first two lists are not reconstructed, so
-- their channels stay NULL.
INSERT INTO sensor_config (sensor_id, version, channel_plan, channels, n_channels, dwell_s, poll_s,
                           created_at, created_by, confirmed_at, note)
SELECT (SELECT id FROM sensors WHERE name = 'pi-fri'), v.version, v.plan, v.channels, v.n, 0.2, 30,
       v.at::timestamptz, 'seed', v.at::timestamptz, v.note
FROM (VALUES
  (1, 'auto', NULL::text[], 91, '2026-09-15 13:53:07+00',
   'Kismet''s autodetected list for rtw88_8821cu'),
  (2, 'custom', NULL::text[], 90, '2026-09-26 22:45:26+00',
   'Autodetected list without 165HT40-; only 45 entries visited (Kismet hop stride 4, gcd(90, 4) = 2)'),
  (3, 'all_89', ARRAY['1', '1HT40+', '2', '3', '4', '5', '6', '6HT40-', '6HT40+', '7', '8', '9', '10',
    '11', '11HT40-', '12', '13', '36', '36HT40+', '36VHT80', '40', '40HT40-', '40VHT80', '44',
    '44HT40+', '44VHT80', '48', '48HT40-', '48VHT80', '52', '52HT40+', '52VHT80', '56',
    '56HT40-', '56VHT80', '60', '60HT40+', '60VHT80', '64', '64HT40-', '64VHT80', '100',
    '100HT40+', '100VHT80', '104', '104HT40-', '104VHT80', '108', '108HT40+', '108VHT80',
    '112', '112HT40-', '112VHT80', '116', '116HT40+', '116VHT80', '120', '120HT40-',
    '120VHT80', '124', '124HT40+', '124VHT80', '128', '128HT40-', '128VHT80', '132',
    '132HT40+', '132VHT80', '136', '136HT40-', '136VHT80', '140', '140VHT80', '144',
    '144HT40-', '144VHT80', '149', '149HT40+', '149VHT80', '153', '153HT40-', '153VHT80',
    '157', '157HT40+', '157VHT80', '161', '161HT40-', '161VHT80', '165'], 89, '2026-09-27 13:04:57+00',
   'Without 165HT40- and 140HT40-; prime length, every entry visited')
) AS v(version, plan, channels, n, at, note)
ON CONFLICT DO NOTHING;

SELECT kind, count(*) AS annotations FROM sensor_annotations GROUP BY 1 ORDER BY 1;
SELECT version, channel_plan, n_channels, confirmed_at FROM sensor_config ORDER BY version;
COMMIT;
