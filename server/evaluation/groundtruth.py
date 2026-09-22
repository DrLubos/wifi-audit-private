"""The ground-truth trial log: load it, and (optionally) corroborate it from pcaps.

One CSV row per staged trial. The wrapper on the attacker device appends the
first columns as it runs; `truth-check` fills the pcap-derived columns later.
The CSV is the source of truth for the report - the pcap only verifies the
attacker's own frame count and provides the precise first-frame timestamp that
anchors latency (never trust only the attack script's clock).

Columns
  trial_id        unique id, e.g. L03 (block letter + number); sorts sensibly
  type            detector under test: 'deauth_flood' (later 'evil_twin')
  variant         fast | moderate | slow | clean   (clean = a control window)
  channel_config  locked | hopping
  target_bssid    the OWN test AP; a 'clean' row may leave it blank
  start_utc       trial start (ISO 8601, UTC); when the wrapper began the attack
  stop_utc        trial stop
  frames_claimed  frames the attack tool reported sending (blank for clean)
  pcap_file       path to the per-trial capture (blank if none)
  notes           free text
Enriched by truth-check (blank until then; if present, they are authoritative)
  first_frame_utc the first deauth/disassoc frame to target_bssid in the pcap
  last_frame_utc  the last such frame
  pcap_frames     count of such frames in the pcap
  frame_count_ok  'ok' | 'MISMATCH' | '' - claimed vs pcap within tolerance

Only the standard library is needed to LOAD the log. Reading a pcap needs tshark
(a subprocess), kept out of the load path so the report runs where only the
database is reachable (e.g. the api container).
"""

import csv
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

from detection.common import UTC, iso, parse_when

VARIANTS = ("fast", "moderate", "slow", "clean")
CHANNEL_CONFIGS = ("locked", "hopping")
ATTACK_VARIANTS = ("fast", "moderate", "slow")     # everything except the control

FIELDS = ["trial_id", "type", "variant", "channel_config", "target_bssid",
          "start_utc", "stop_utc", "frames_claimed", "pcap_file", "notes",
          "first_frame_utc", "last_frame_utc", "pcap_frames", "frame_count_ok"]

# claimed vs pcap frame count agree within this fraction (plus a small absolute
# floor): the tool and a nearby capture never match to the frame.
FRAME_TOLERANCE_FRAC = 0.15
FRAME_TOLERANCE_ABS = 5


class GroundTruthError(Exception):
    pass


@dataclass
class Trial:
    trial_id: str
    type: str
    variant: str
    channel_config: str
    target_bssid: str | None
    start: datetime
    stop: datetime
    frames_claimed: int | None = None
    pcap_file: str | None = None
    notes: str = ""
    first_frame: datetime | None = None
    last_frame: datetime | None = None
    pcap_frames: int | None = None
    frame_count_ok: str = ""
    # filled by metrics.match(); detector-private, not persisted here
    extra: dict = field(default_factory=dict)

    @property
    def is_attack(self):
        return self.variant in ATTACK_VARIANTS

    @property
    def duration_s(self):
        return (self.stop - self.start).total_seconds()

    @property
    def anchor(self):
        """The latency anchor: the true first-frame time when a pcap gave one,
        else the logged start (coarser, flagged in the report)."""
        return self.first_frame or self.start


def _int_or_none(s):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        raise GroundTruthError("not an integer: %r" % s) from None


def _bssid(s):
    s = (s or "").strip().upper()
    return s or None


def load_trials(path):
    """Read the ground-truth CSV into Trial objects, validated and time-ordered."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise GroundTruthError("no trials in %s" % path)
    trials = []
    seen = set()
    for i, r in enumerate(rows, 1):
        tid = (r.get("trial_id") or "").strip()
        if not tid or tid.startswith("#"):
            continue                              # blank or a comment/authorisation line

        if tid in seen:
            raise GroundTruthError("duplicate trial_id %r" % tid)
        seen.add(tid)
        variant = (r.get("variant") or "").strip().lower()
        if variant not in VARIANTS:
            raise GroundTruthError("trial %s: variant %r not in %s" % (tid, variant, VARIANTS))
        cfg = (r.get("channel_config") or "").strip().lower()
        if cfg not in CHANNEL_CONFIGS:
            raise GroundTruthError("trial %s: channel_config %r not in %s" % (tid, cfg, CHANNEL_CONFIGS))
        try:
            start = parse_when(r["start_utc"])
            stop = parse_when(r["stop_utc"])
        except (KeyError, ValueError) as e:
            raise GroundTruthError("trial %s: bad start/stop (%s)" % (tid, e)) from None
        if stop <= start:
            raise GroundTruthError("trial %s: stop_utc <= start_utc" % tid)
        target = _bssid(r.get("target_bssid"))
        if variant in ATTACK_VARIANTS and target is None:
            raise GroundTruthError("trial %s: attack trial needs a target_bssid" % tid)
        trials.append(Trial(
            trial_id=tid, type=(r.get("type") or "deauth_flood").strip(),
            variant=variant, channel_config=cfg, target_bssid=target,
            start=start, stop=stop,
            frames_claimed=_int_or_none(r.get("frames_claimed")),
            pcap_file=(r.get("pcap_file") or "").strip() or None,
            notes=(r.get("notes") or "").strip(),
            first_frame=parse_when(r["first_frame_utc"]) if (r.get("first_frame_utc") or "").strip() else None,
            last_frame=parse_when(r["last_frame_utc"]) if (r.get("last_frame_utc") or "").strip() else None,
            pcap_frames=_int_or_none(r.get("pcap_frames")),
            frame_count_ok=(r.get("frame_count_ok") or "").strip()))
    trials.sort(key=lambda t: (t.start, t.trial_id))
    return trials


def overlaps(trials):
    """Pairs of trials whose [start, stop] overlap - a logging mistake that
    would make a detection ambiguous. Returns a list of (id, id)."""
    bad = []
    ordered = sorted(trials, key=lambda t: t.start)
    for a, b in zip(ordered, ordered[1:]):
        if b.start < a.stop:
            bad.append((a.trial_id, b.trial_id))
    return bad


# --- pcap corroboration (tshark; used by truth-check, not by the report) ------

def frame_count_ok(claimed, counted):
    if claimed is None or counted is None:
        return ""
    tol = max(FRAME_TOLERANCE_ABS, FRAME_TOLERANCE_FRAC * max(claimed, counted))
    return "ok" if abs(claimed - counted) <= tol else "MISMATCH"


def read_pcap(pcap_file, target_bssid):
    """(first_frame, last_frame, count) of deauth/disassoc frames whose addr2
    (transmitter) or addr3 (BSSID) is target_bssid. Needs tshark on PATH.

    subtype 12 = deauthentication, 10 = disassociation; frame.time_epoch is the
    capture clock (chrony-synced on the attacker box)."""
    target = target_bssid.lower()
    cmd = ["tshark", "-r", pcap_file, "-Y",
           "(wlan.fc.type_subtype == 12 || wlan.fc.type_subtype == 10) && "
           "(wlan.ta == %s || wlan.bssid == %s)" % (target, target),
           "-T", "fields", "-e", "frame.time_epoch"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise GroundTruthError("tshark not found; run truth-check where tshark is installed") from None
    except subprocess.CalledProcessError as e:
        raise GroundTruthError("tshark failed on %s: %s" % (pcap_file, e.stderr.strip())) from None
    times = sorted(float(x) for x in out.split() if x.strip())
    if not times:
        return None, None, 0
    first = datetime.fromtimestamp(times[0], UTC)
    last = datetime.fromtimestamp(times[-1], UTC)
    return first, last, len(times)


def enrich_from_pcaps(trials, log=lambda m: None):
    """Fill first_frame/last_frame/pcap_frames/frame_count_ok from each attack
    trial's pcap. Mutates the trials; returns the count enriched."""
    n = 0
    for t in trials:
        if not t.is_attack or not t.pcap_file:
            continue
        first, last, count = read_pcap(t.pcap_file, t.target_bssid)
        t.first_frame, t.last_frame, t.pcap_frames = first, last, count
        t.frame_count_ok = frame_count_ok(t.frames_claimed, count)
        n += 1
        if t.frame_count_ok == "MISMATCH":
            log("  %s: claimed %s frames, pcap has %d (MISMATCH)"
                % (t.trial_id, t.frames_claimed, count))
        elif count == 0:
            log("  %s: no deauth/disassoc frames for %s in %s"
                % (t.trial_id, t.target_bssid, t.pcap_file))
    return n


def write_trials(path, trials):
    """Write the (enriched) trials back as CSV, all FIELDS columns."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for t in trials:
            w.writerow({
                "trial_id": t.trial_id, "type": t.type, "variant": t.variant,
                "channel_config": t.channel_config, "target_bssid": t.target_bssid or "",
                "start_utc": iso(t.start), "stop_utc": iso(t.stop),
                "frames_claimed": "" if t.frames_claimed is None else t.frames_claimed,
                "pcap_file": t.pcap_file or "", "notes": t.notes,
                "first_frame_utc": iso(t.first_frame) if t.first_frame else "",
                "last_frame_utc": iso(t.last_frame) if t.last_frame else "",
                "pcap_frames": "" if t.pcap_frames is None else t.pcap_frames,
                "frame_count_ok": t.frame_count_ok})
