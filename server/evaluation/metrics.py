"""Join staged trials against detections and compute the evaluation statistics.

Everything here is pure: it takes Trial objects (groundtruth.py) and Detection
objects (built from a `detections` row, or synthetically in the tests) and
returns numbers. The database read lives in __main__.py; these functions never
touch it, so the tests run stdlib-only.

The match window is deliberately tighter than the detector's 300 s episode gap
(see the plan): trials are spaced >= 9 min, so a small pre/post slack attributes
every detection to exactly one trial. The FP guard-band is separate and wider.
"""

import math
from dataclasses import dataclass, field
from datetime import timedelta

from detection.common import UTC, parse_when

# Match a detection to a trial when its earliest evidence timestamp is in
# [start - PRE, stop + POST]. PRE absorbs attacker<->Pi clock skew and the lag
# between logging start_utc and the first frame; POST absorbs a trailing poll or
# throttled alert dated after the last frame (~2 poll intervals).
MATCH_SLACK_PRE = timedelta(seconds=15)
MATCH_SLACK_POST = timedelta(seconds=60)
# When counting false positives on OTHER APs, ignore anything within this of any
# trial so attack-adjacent spillover is never miscounted as organic.
FP_GUARD = timedelta(seconds=300)


@dataclass
class Detection:
    """One `detections` row, reduced to what the evaluation needs."""
    id: int
    bssid: str | None                 # upper-case
    ssid: str | None
    device_key: str | None
    severity: str
    evidence: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row):
        ev = row.get("evidence") or {}
        mac = row.get("mac")
        bssid = None
        if mac:
            bssid = str(mac).upper()
        elif (ev.get("ap") or {}).get("bssid"):
            bssid = str(ev["ap"]["bssid"]).upper()
        return cls(id=row["id"], bssid=bssid, ssid=row.get("ssid"),
                   device_key=row.get("device_key"), severity=row["severity"], evidence=ev)

    @property
    def window_start(self):
        return _ev_ts(self.evidence.get("window_start"))

    @property
    def window_end(self):
        return _ev_ts(self.evidence.get("window_end"))

    @property
    def earliest_ts(self):
        """Earliest dated evidence: episode start, first alert, or first burst."""
        cands = [self.window_start]
        cands.append(_ev_ts((self.evidence.get("alerts") or {}).get("first_ts")))
        times = (self.evidence.get("bursts") or {}).get("times") or []
        if times:
            cands.append(_ev_ts(times[0]))
        cands = [c for c in cands if c is not None]
        return min(cands) if cands else None

    @property
    def rule(self):
        r = self.evidence.get("rules") or {}
        if r.get("alerts") and r.get("bursts"):
            return "A+B"
        if r.get("alerts"):
            return "A"
        if r.get("bursts"):
            return "B"
        return "?"

    @property
    def burst_sources(self):
        return (self.evidence.get("bursts") or {}).get("sources") or {}

    @property
    def mgmt_ratio(self):
        return (self.evidence.get("observed") or {}).get("mgmt_ratio_to_median")


def _ev_ts(v):
    if not v:
        return None
    try:
        return parse_when(v)
    except (ValueError, TypeError):
        return None


# --- statistics ----------------------------------------------------------------

def wilson_ci(k, n, z=1.96):
    """Wilson score interval for k successes in n trials. (lo, hi) in [0, 1],
    or (None, None) for n == 0. Honest for small n where the normal
    approximation is not."""
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# --- matching ------------------------------------------------------------------

def match_trial(trial, detections):
    """Detections attributable to this trial: same subject BSSID and earliest
    evidence within [start - PRE, stop + POST]."""
    if trial.target_bssid is None:
        return []
    lo = trial.start - MATCH_SLACK_PRE
    hi = trial.stop + MATCH_SLACK_POST
    out = []
    for d in detections:
        if d.bssid != trial.target_bssid:
            continue
        ts = d.earliest_ts
        if ts is not None and lo <= ts <= hi:
            out.append(d)
    return out


def latency_seconds(trial, detection):
    """(frame_anchor_s, emit_anchor_s) for a matched detection.
    frame_anchor: dated evidence minus the true first frame (needs a pcap; ~1 s
      for the exact rule). None when the trial has no pcap first-frame.
    emit_anchor: dated evidence minus the logged trial start; bounded below by up
      to one poll interval, so it is the 'operator sees it' latency, not the
      detector's. Never conflate the two."""
    ts = detection.earliest_ts
    if ts is None:
        return None, None
    emit = (ts - trial.start).total_seconds()
    frame = (ts - trial.first_frame).total_seconds() if trial.first_frame else None
    return frame, emit


# --- evaluation ----------------------------------------------------------------

@dataclass
class Cell:
    """One (variant, channel_config) result cell."""
    variant: str
    channel_config: str
    n: int = 0
    detected: int = 0
    severities: list = field(default_factory=list)
    rules: list = field(default_factory=list)
    sources: dict = field(default_factory=lambda: {"last": 0, "counter": 0})
    latency_frame: list = field(default_factory=list)
    latency_emit: list = field(default_factory=list)
    mgmt_ratios: list = field(default_factory=list)

    @property
    def recall(self):
        return self.detected / self.n if self.n else None

    @property
    def wilson(self):
        return wilson_ci(self.detected, self.n)


def evaluate(trials, detections, detector_type):
    """Return (cells, per_trial, fps, strays).

    cells:      dict keyed (variant, channel_config) -> Cell, attack trials only
    per_trial:  list of per-trial dicts (every trial, for the CSV)
    fps:        detections on OTHER APs, in-window, outside every trial guard band
    strays:     detections on a test BSSID that matched no trial (edge/leftover)
    """
    trials = [t for t in trials if t.type == detector_type]
    dets = [d for d in detections if d.evidence.get("detector", detector_type) == detector_type]
    targets = {t.target_bssid for t in trials if t.target_bssid}

    cells = {}
    per_trial = []
    matched_ids = set()
    for t in trials:
        matches = match_trial(t, dets)
        for d in matches:
            matched_ids.add(d.id)
        best = _pick(matches)
        row = {
            "trial_id": t.trial_id, "type": t.type, "variant": t.variant,
            "channel_config": t.channel_config, "target_bssid": t.target_bssid,
            "detected": bool(matches), "n_detections": len(matches),
            "severity": best.severity if best else "",
            "rule": best.rule if best else "",
            "sources": best.burst_sources if best else {},
            "det_id": best.id if best else None,
            "pcap_frames": t.pcap_frames, "frames_claimed": t.frames_claimed,
            "frame_count_ok": t.frame_count_ok,
            "latency_frame_s": None, "latency_emit_s": None, "mgmt_ratio": None,
        }
        if best:
            fr, em = latency_seconds(t, best)
            row["latency_frame_s"] = fr
            row["latency_emit_s"] = em
            row["mgmt_ratio"] = best.mgmt_ratio
        per_trial.append(row)

        if not t.is_attack:
            continue
        cell = cells.setdefault((t.variant, t.channel_config), Cell(t.variant, t.channel_config))
        cell.n += 1
        if best:
            cell.detected += 1
            cell.severities.append(best.severity)
            cell.rules.append(best.rule)
            for k in ("last", "counter"):
                cell.sources[k] += int(best.burst_sources.get(k) or 0)
            fr, em = latency_seconds(t, best)
            if fr is not None:
                cell.latency_frame.append(fr)
            if em is not None:
                cell.latency_emit.append(em)
            if best.mgmt_ratio is not None:
                cell.mgmt_ratios.append(float(best.mgmt_ratio))

    # False positives: a detection on a non-target BSSID, outside every guard band.
    fps, strays = [], []
    for d in dets:
        if d.id in matched_ids:
            continue
        if d.bssid in targets:
            strays.append(d)
            continue
        ts = d.earliest_ts
        if ts is None:
            continue
        if _near_any_trial(ts, trials):
            continue
        fps.append(d)
    return cells, per_trial, fps, strays


_SEV_ORDER = ("info", "low", "medium", "high", "critical")


def _pick(matches):
    """The representative detection for a trial: the most severe, then earliest."""
    if not matches:
        return None
    return sorted(matches, key=lambda d: (
        -_SEV_ORDER.index(d.severity) if d.severity in _SEV_ORDER else 0,
        d.earliest_ts or parse_when("2100-01-01")))[0]


def _near_any_trial(ts, trials):
    for t in trials:
        if t.start - FP_GUARD <= ts <= t.stop + FP_GUARD:
            return True
    return False


def channel_hop_penalty(cells):
    """Per variant: (locked_recall, hopping_recall, penalty). penalty is
    locked - hopping (positive = hopping misses more)."""
    out = {}
    variants = sorted({v for (v, _c) in cells})
    for v in variants:
        lk = cells.get((v, "locked"))
        hp = cells.get((v, "hopping"))
        out[v] = (lk.recall if lk else None, hp.recall if hp else None,
                  (lk.recall - hp.recall) if (lk and hp and lk.recall is not None
                                              and hp.recall is not None) else None)
    return out


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2
