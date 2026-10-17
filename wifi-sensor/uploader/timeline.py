"""The true time line of the buffer's polls (server/docs/live-upload.md 8.3).

The collector (buffer v8) records per poll the wall clock (ts, integer
seconds), CLOCK_MONOTONIC (mono), the boot and whether chrony was synchronised;
per boot one anchor at its first synchronised poll. A boot without NTP runs on
a stale clock (the Pi has no RTC), so before an upload every poll and every
wall-clock value in its rows is placed on the true time line:

- a synchronised poll (or one written before v8): its ts as recorded;
- an unsynchronised poll of an anchored boot: true ts = epoch + mono, with the
  boot's true epoch = anchor wall - anchor mono (offset = true - raw);
- an unsynchronised poll of the current boot without an anchor: held back
  (no upload until the boot gets one - NTP, or the server's time);
- one of an earlier boot that never got an anchor: time unverified, never
  uploaded, counted.

Kismet's own times (an observation's last_time and disconnects_last, an
alert's ts) of a boot are stale iff earlier than the stale clock's reading at
the step (epoch_stale + step_mono; for a server anchor there was no step -
every value of the boot is stale); a stale one moves by the boot's delta
(epoch_true - epoch_stale). A delta below MIN_SHIFT_S moves nothing (within
the target), a negative one (a clock that was ahead) moves no Kismet time.
Device-level values (first/last seen, configuration-change and load times)
have no boot; one that falls in a boot's stale window is not placeable and is
replaced or left out by the batch builder.

Standard library only (tests/test_timeline.py).
"""

MIN_SHIFT_S = 10.0
WINDOW_MARGIN_S = 120.0       # around a stale stretch: a poll interval + the poll's own duration


class Boot:
    """What is known about one boot of the buffer."""

    def __init__(self, boot_id, anchor=None, source=None, stale_ts=None, stale_mono=None,
                 unsynced_mono=(None, None)):
        self.boot_id = boot_id
        self.source = source                       # 'ntp' | 'server' | None
        self.epoch_true = None if anchor is None else anchor[0] - anchor[1]   # (wall, mono)
        self.step_mono = None if anchor is None or source == "server" else anchor[1]
        # the stale epoch: raw ts - mono of the boot's first unsynchronised poll
        self.epoch_stale = None if stale_ts is None else stale_ts - stale_mono
        self.unsynced_mono = unsynced_mono         # (min, max) mono of its unsynchronised polls

    @property
    def anchored(self):
        return self.epoch_true is not None

    @property
    def delta(self):
        if self.epoch_true is None or self.epoch_stale is None:
            return None
        return self.epoch_true - self.epoch_stale

    def stale_window(self):
        """(from, to) on the stale clock in which this boot's unsynchronised
        polls wrote their times, None when it had none."""
        if self.epoch_stale is None or self.unsynced_mono[0] is None:
            return None
        return (self.epoch_stale + self.unsynced_mono[0] - WINDOW_MARGIN_S,
                self.epoch_stale + self.unsynced_mono[1] + WINDOW_MARGIN_S)

    def to_payload(self, polls_corrected, offsets):
        return {"boot_id": self.boot_id, "source": self.source or "ntp", "epoch_true": self.epoch_true,
                "epoch_stale": self.epoch_stale, "step_mono": self.step_mono,
                "polls_corrected": polls_corrected,
                "offset_min_s": min(offsets) if offsets else None,
                "offset_max_s": max(offsets) if offsets else None}


HOLD, UNVERIFIED, SEND = "hold", "unverified", "send"


def poll_fate(poll, boot, current_boot):
    """SEND, HOLD (wait for the anchor) or UNVERIFIED (never placeable).
    poll: dict with clock_synced, boot_id."""
    if poll.get("clock_synced") is None or poll["clock_synced"]:
        return SEND
    if boot is not None and boot.anchored:
        return SEND
    if poll.get("boot_id") == current_boot:
        return HOLD
    return UNVERIFIED


def place_poll(poll, boot):
    """(true ts, offset) of a poll to SEND; offset None for a synchronised one."""
    if poll.get("clock_synced") is None or poll["clock_synced"] or poll.get("mono") is None:
        return poll["ts"], None
    true_ts = int(round(boot.epoch_true + poll["mono"]))
    return true_ts, float(true_ts - poll["ts"])


def place_kismet(value, boot, poll_offset):
    """A Kismet time (frame or alert time) written by a poll of `boot`.
    poll_offset: the offset of that poll (None = it was synchronised)."""
    if value is None or boot is None or not boot.anchored or boot.epoch_stale is None:
        return value
    delta = boot.delta
    if delta is None or delta < MIN_SHIFT_S:
        return value
    if boot.step_mono is None:                   # server anchor: no step, every value is stale
        stale = True
    else:
        stale = value < boot.epoch_stale + boot.step_mono
    if not stale:
        return value
    shifted = value + delta
    return int(round(shifted)) if isinstance(value, int) else shifted


def placeable(value, boots):
    """A device-level value (no boot known) as it can be sent: unchanged
    outside every stale window; inside one of an anchored boot with a
    positive delta: shifted; else None (not placeable - left out)."""
    if value is None:
        return None
    for b in boots:
        w = b.stale_window()
        if w is None or not w[0] <= value <= w[1]:
            continue
        if b.anchored and b.delta is not None and b.delta >= MIN_SHIFT_S:
            return int(round(value + b.delta))
        if b.anchored and b.delta is not None and abs(b.delta) < MIN_SHIFT_S:
            return value
        return None
    return value
