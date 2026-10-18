-- Per heartbeat (stored at most once a minute): clock offset, RTT, backlog, sensor state.
-- clock_method: how clock_offset_s was computed (README.md - changed on 2026-10-17).
COPY (
SELECT s.name AS sensor, h.received_at, h.sensor_ts, h.clock_offset_s,
       CASE WHEN h.received_at < '2026-10-17 21:08:13+00' THEN 'recv_minus_send_minus_half_rtt'
            WHEN h.received_at < '2026-10-17 21:32:30+00' THEN 'recv_minus_send_early_stamp'
            ELSE 'recv_minus_send' END AS clock_method,
       h.rtt_ms, h.chrony_synced, h.chrony_offset_s, h.boot_id, h.uptime_s,
       h.last_poll_ts, h.backlog_polls, h.backlog_oldest_ts, h.held_polls, h.unverified_polls,
       h.buffer_oldest_ts, h.buffer_bytes, h.disk_free_bytes, h.uploader_version
FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id
WHERE s.name = :'sensor' AND h.received_at >= :'from'::timestamptz AND h.received_at < :'to'::timestamptz
ORDER BY h.received_at
) TO STDOUT WITH (FORMAT csv, HEADER);
