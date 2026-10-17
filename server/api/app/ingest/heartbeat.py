"""Heartbeat helpers (docs/live-upload.md 4.5, 7, 8.4): the server's view of
the sensor's clock, the history cadence, the adapter check and the
retention warning. Standard library only (tests/test_ingest_logic.py).
"""

HISTORY_GAP_S = 55            # sensor_heartbeats: at most one row a minute (N5)
RETENTION_MARGIN_DAYS = 2     # D10: warn two days before the sensor's retention


def clock_offset_s(received_at, sensor_ts, rtt_ms):
    """received_at - sensor_ts - RTT/2 (seconds; positive = the sensor is behind)."""
    return received_at - sensor_ts - (rtt_ms or 0) / 2000.0


def history_due(last_stored_at, now):
    return last_stored_at is None or (now - last_stored_at).total_seconds() >= HISTORY_GAP_S


def adapter_action(stored, reported):
    """'none' | 'fill' | 'change' for the adapter a heartbeat reports.

    Identity = USB VID:PID + MAC; driver, chipset and interface are display
    only. No adapter (the interface is gone, e.g. the 2026-10-06 loss) is
    never a change. A field known on both sides that differs is a change - a
    hardware_change boundary. A field the stored record lacks (the seeded
    record has neither, D8) is filled without a boundary.
    """
    if not reported or (not reported.get("usb_id") and not reported.get("mac")):
        return "none"
    if not stored:
        return "fill"
    fill = False
    for k in ("usb_id", "mac"):
        new, old = reported.get(k), stored.get(k)
        if new is None:
            continue
        if old is None:
            fill = True
        elif str(old).lower() != str(new).lower():
            return "change"
    return "fill" if fill else "none"


def retention_risk(hb, now):
    """D10: the oldest unacknowledged poll is older than retention - 2 days."""
    oldest, days = hb.get("backlog_oldest_ts"), hb.get("retention_days")
    if oldest is None or not days or not hb.get("backlog_polls"):
        return False
    return oldest < now.timestamp() - (days - RETENTION_MARGIN_DAYS) * 86400
