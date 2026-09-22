"""evil_twin - rogue access points cloning a known AP, per sensor and AP.

Two signals over [--from, --to) widened by the episode gap. Signal (b) is the
headline; signal (a) is a deliberately subordinate corroborator.

  (b) RSSI baseline deviation of a KNOWN BSSID from its own history. An exact-
      BSSID clone (the classic evil twin) is the SAME Kismet device - same MAC,
      same device_key - so the attacker's frames land in that BSSID's own
      observations.rssi stream. A fixed continuous sensor can therefore notice
      what a mobile one-shot audit cannot: the signal sustainedly leaving the
      band this AP has occupied for days. This has no Kismet equivalent and is
      the detector's contribution. Two facets of the same windowed statistic:

        median_shift    - the median of a sliding window of W observations
                          leaves ap_baselines.rssi_median by more than
                          max(k * rssi_robust_sd, --floor-db). The window MEDIAN,
                          not a single reading: a per-reading 3-sigma rule flags
                          ~8 % of genuine readings (analysis/rssi_stability.py),
                          the window median almost none.
        spread_inflation- the window's own robust spread (MAD) rises well above
                          the baseline robust sigma: a BSSID heard from two
                          positions at once is bimodal even when the medians
                          cross.

      A deviation must PERSIST (>= --persistence deviating observations within
      --persist-window) to be a detection - one noisy poll never fires. A twin
      that overpowers the real AP shows direction 'stronger'; that on a trusted
      AP is the strongest signature and grades up.

  (a) Unknown BSSID for an established SSID (subordinate). An AP advertising an
      SSID whose trusted BSSID set is established, with a BSSID not in that set.
      This is the same class as Kismet's native APSPOOF alert (which needs a
      hand-maintained validmacs list and is not configured here, so it never
      fires); the value it adds over Kismet is operational, not novel, so it is
      subordinate to (b) and given only a deterministic membership check. The
      trusted set is the operator whitelist (ap_baselines.trusted) or, for the
      seeded false-positive audit before any approval exists, the BSSIDs first
      seen in the baseline window (--trusted-source). To avoid the false
      positives the naive analysis produced (findings.md sec 4): the SSID is
      matched EXACTLY (never by prefix, so IK-WIFI does not sweep in
      IK-WIFI-DOT1X); OUI and encryption are corroborating SCORE, never a
      trigger, so a legitimate second-vendor infrastructure BSSID (same OUI
      family, same crypt) is recorded at low, not high; a random/transient
      BSSID never triggers.

An episode of one AP is graded, and severities escalate when (a) and (b)
corroborate (an unknown BSSID for an SSID whose trusted member is also
deviating). Severity: (b) by deviation magnitude (low/medium/high), +1 level
when a stronger clone sits on a trusted AP or (a) corroborates; (a) low when the
OUI and crypt match the trusted set, medium otherwise, +1 when close/strong or
(b) corroborates.

Everything is read-only (SET TRANSACTION READ ONLY); the only writes are the
detections rows through detections.write().
"""

import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import common, detections
from .common import POLL_INTERVAL_S, fmt_dur, fmt_ts, iso

TYPE = "evil_twin"
VERSION = 1
SEVERITIES = ("info", "low", "medium", "high", "critical")
APSPOOF_HEADERS = ("APSPOOF",)


# --- SQL ----------------------------------------------------------------------------
# Every statement is a SELECT; writes go through detections.write().

# Signal (b): observations of every AP that has a qualifying baseline, joined to
# that baseline. rssi is Kismet sig_last (one frame per poll), NULL when absent.
OBS_SQL = """
SELECT o.device_key, o.ts, o.rssi,
       upper(d.mac::text) AS bssid, d.ssid, d.manuf, d.crypt,
       trunc(d.mac)::text AS oui,
       (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00' AS random_bssid,
       b.rssi_median, b.rssi_robust_sd, b.n_obs AS base_n_obs, b.n_days AS base_n_days,
       b.window_start AS base_from, b.window_end AS base_to,
       coalesce(b.trusted, false) AS trusted
FROM observations o
JOIN devices d      ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
JOIN ap_baselines b ON b.sensor_id = o.sensor_id AND b.device_key = o.device_key
WHERE o.sensor_id = %(sid)s AND d.type = 'ap' AND o.rssi IS NOT NULL
  AND b.rssi_median IS NOT NULL AND b.rssi_robust_sd IS NOT NULL
  AND o.ts >= %(scan_from)s AND o.ts < %(scan_to)s
ORDER BY o.device_key, o.ts"""

# Signal (a): every AP advertising a named SSID, with its in-window presence and
# median RSSI. device_config_history SSIDs are unioned in analyse().
INVENTORY_SQL = """
SELECT d.device_key, upper(d.mac::text) AS bssid, trunc(d.mac)::text AS oui, d.manuf,
       d.ssid, d.crypt,
       (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00' AS random_bssid,
       coalesce(b.trusted, false) AS trusted,
       d.first_seen, d.last_seen,
       count(o.rssi) FILTER (WHERE o.ts >= %(scan_from)s AND o.ts < %(scan_to)s) AS win_obs,
       min(o.ts)     FILTER (WHERE o.ts >= %(scan_from)s AND o.ts < %(scan_to)s) AS win_first,
       max(o.ts)     FILTER (WHERE o.ts >= %(scan_from)s AND o.ts < %(scan_to)s) AS win_last,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY o.rssi)
         FILTER (WHERE o.rssi IS NOT NULL AND o.ts >= %(scan_from)s AND o.ts < %(scan_to)s) AS win_median
FROM devices d
LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
LEFT JOIN observations o ON o.sensor_id = d.sensor_id AND o.device_key = d.device_key
WHERE d.sensor_id = %(sid)s AND d.type = 'ap' AND d.ssid IS NOT NULL AND d.ssid <> ''
GROUP BY d.device_key, d.mac, d.manuf, d.ssid, d.crypt, b.trusted, d.first_seen, d.last_seen"""

# SSIDs each AP advertised in the past (so a twin that has since changed its SSID
# is still matched to the protected name it cloned).
HISTORY_SQL = """
SELECT device_key, ssid FROM device_config_history
WHERE sensor_id = %(sid)s AND ssid IS NOT NULL AND ssid <> ''"""

# APSPOOF alerts corroborating signal (a), joined to the AP by transmitter_mac.
ALERTS_SQL = """
SELECT a.id, a.ts, upper(a.transmitter_mac::text) AS bssid
FROM alerts a
WHERE a.sensor_id = %(sid)s AND a.header = ANY(%(headers)s)
  AND a.ts IS NOT NULL AND a.transmitter_mac IS NOT NULL
  AND a.ts >= %(scan_from)s AND a.ts < %(scan_to)s"""


# --- data ---------------------------------------------------------------------------

@dataclass
class Params:
    gap_s: float = 300
    window_w: int = 10                  # observations per sliding window (b)
    k: float = 6.0                      # median-shift threshold in robust sigmas
    floor_db: float = 8.0               # absolute floor on the median shift (dB)
    sd_floor_db: float = 1.0            # robust sigma is never treated below this
    spread_k: float = 3.0              # spread-inflation: window MAD vs k*baseline sigma
    spread_floor_db: float = 6.0
    persistence: int = 6                # deviating observations required within...
    persist_window_s: float = 900       # ...this sliding window (b)
    med_dev_db: float = 12.0            # (b) severity thresholds on the max deviation
    high_dev_db: float = 20.0
    trusted_source: str = "whitelist"   # whitelist | baseline
    baseline_hours: float = 24          # baseline-mode cutoff = --from + this
    persist_hours: float = 1.0          # (a) sustained if span >= this OR ...
    persist_obs: int = 20               # ... this many in-window observations
    close_dbm: float = -60              # (a) 'close/strong' escalation
    strong_db: float = 10

    @classmethod
    def from_args(cls, args):
        return cls(gap_s=args.gap, window_w=args.window, k=args.k, floor_db=args.floor_db,
                   spread_k=args.spread_k, persistence=args.persistence,
                   persist_window_s=args.persist_window, med_dev_db=args.med_dev, high_dev_db=args.high_dev,
                   trusted_source=args.trusted_source, baseline_hours=args.baseline_hours,
                   persist_hours=args.persist_hours, persist_obs=args.persist_obs)

    def as_dict(self):
        return {"gap_s": self.gap_s, "window_w": self.window_w, "k": self.k,
                "floor_db": self.floor_db, "spread_k": self.spread_k,
                "persistence": self.persistence, "persist_window_s": self.persist_window_s,
                "med_dev_db": self.med_dev_db, "high_dev_db": self.high_dev_db,
                "trusted_source": self.trusted_source, "baseline_hours": self.baseline_hours,
                "persist_hours": self.persist_hours, "persist_obs": self.persist_obs}


@dataclass
class Deviation:
    """One sliding window whose robust statistic left the AP's baseline."""
    ts: datetime                        # the window's last observation
    device_key: str
    window_median: float
    window_mad: float
    dev_db: float                       # window_median - baseline_median (signed)
    facet: str                          # median_shift | spread_inflation | both


@dataclass
class Unknown:
    """One AP advertising an established SSID with a non-trusted BSSID (signal a)."""
    device_key: str
    bssid: str
    oui: str
    manuf: str | None
    ssid: str
    crypt: str | None
    random_bssid: bool
    first_seen: datetime
    win_first: datetime | None
    win_last: datetime | None
    win_obs: int
    win_median: float | None
    oui_in_trusted: bool
    crypt_matches: bool
    trusted_bssids: list
    trusted_ouis: list
    dominant_crypt: str | None
    group_median: float | None
    apspoof_ids: list = field(default_factory=list)
    # filled by assess_unknown()
    sustained: bool = False
    close: bool = False
    corroborated_b: bool = False
    severity: str | None = None
    finding: object = None

    @property
    def span_s(self):
        if self.win_first is None or self.win_last is None:
            return 0.0
        return (self.win_last - self.win_first).total_seconds()


@dataclass
class Episode:
    """A run of deviating windows of one AP (signal b)."""
    device_key: str
    bssid: str | None = None
    ssid: str | None = None
    trusted: bool = False
    baseline: dict = field(default_factory=dict)
    deviations: list = field(default_factory=list)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    # filled by assess()
    max_dev_db: float = 0.0
    direction: str | None = None
    facet: str | None = None
    max_in_window: int = 0
    rule_b: bool = False
    corroborated_a: bool = False
    severity: str | None = None
    finding: object = None

    def add(self, dev):
        self.deviations.append(dev)
        self.first_ts = dev.ts if self.first_ts is None else min(self.first_ts, dev.ts)
        self.last_ts = dev.ts if self.last_ts is None else max(self.last_ts, dev.ts)

    @property
    def duration_s(self):
        return (self.last_ts - self.first_ts).total_seconds()

    @property
    def emitted(self):
        return self.rule_b

    def overlaps(self, frm, to):
        return self.last_ts >= frm and self.first_ts < to

    def label(self):
        return "%s (%s)" % (self.ssid if self.ssid else "<hidden>", self.bssid or self.device_key)


# --- pure logic (unit-tested without a database) ----------------------------------------

def _median(xs):
    return statistics.median(xs)


def _mad(xs, med):
    return statistics.median([abs(x - med) for x in xs])


def windowed_deviations(readings, base_median, base_robust_sd, params, device_key="?"):
    """Deviating trailing windows of one AP. readings: [(ts, rssi)] sorted by ts.
    A window ending at observation i covers the W readings up to i; it deviates
    when the window median leaves the baseline by more than max(k*sigma, floor)
    (median_shift) or the window MAD exceeds max(spread_k*sigma, spread_floor)
    (spread_inflation)."""
    sigma = max(float(base_robust_sd), params.sd_floor_db)
    shift_th = max(params.k * sigma, params.floor_db)
    spread_th = max(params.spread_k * sigma, params.spread_floor_db)
    out = []
    vals = [r[1] for r in readings]
    for i in range(params.window_w - 1, len(readings)):
        win = vals[i - params.window_w + 1:i + 1]
        wmed = _median(win)
        wmad = _mad(win, wmed)
        dev = wmed - base_median
        shift = abs(dev) >= shift_th
        spread = wmad >= spread_th
        if not (shift or spread):
            continue
        facet = "both" if shift and spread else ("median_shift" if shift else "spread_inflation")
        out.append(Deviation(ts=readings[i][0], device_key=device_key, window_median=wmed,
                             window_mad=wmad, dev_db=dev, facet=facet))
    return out


def build_episodes(deviations, gap_s):
    """Merge one AP's deviating windows into episodes; a silence > gap_s closes
    one. Deviations must be time-sorted per device."""
    by_dev = defaultdict(list)
    for d in deviations:
        by_dev[d.device_key].append(d)
    episodes = []
    for device_key, devs in by_dev.items():
        devs.sort(key=lambda d: d.ts)
        current = None
        for d in devs:
            if current is None or (d.ts - current.last_ts).total_seconds() > gap_s:
                current = Episode(device_key=device_key)
                episodes.append(current)
            current.add(d)
    episodes.sort(key=lambda e: (e.first_ts, e.device_key))
    return episodes


def max_in_window(times, window_s):
    """Largest number of the (sorted) timestamps within any window_s span."""
    times = sorted(times)
    best = j = 0
    for i, t in enumerate(times):
        while (t - times[j]).total_seconds() > window_s:
            j += 1
        best = max(best, i - j + 1)
    return best


def grade_b(max_dev_db, direction, trusted, corroborated_a, params):
    """Signal (b) severity by deviation magnitude, +1 level for a stronger clone
    on a trusted AP or when (a) corroborates."""
    if max_dev_db >= params.high_dev_db:
        level = "high"
    elif max_dev_db >= params.med_dev_db:
        level = "medium"
    else:
        level = "low"
    bump = 0
    if direction == "stronger" and trusted:
        bump += 1
    if corroborated_a:
        bump += 1
    return SEVERITIES[min(SEVERITIES.index(level) + bump, len(SEVERITIES) - 1)]


def grade_a(oui_in_trusted, crypt_matches, close, corroborated_b):
    """Signal (a) severity: low when OUI and crypt match the trusted set (the
    legit second-vendor case), medium otherwise, +1 for close/strong or (b)."""
    level = "low" if (oui_in_trusted and crypt_matches) else "medium"
    bump = (1 if close else 0) + (1 if corroborated_b else 0)
    return SEVERITIES[min(SEVERITIES.index(level) + bump, len(SEVERITIES) - 1)]


def assess(ep, params, baseline, bssid, ssid, trusted):
    """Fill an episode's (b) assessment. baseline: the ap_baselines row."""
    ep.baseline = baseline or {}
    ep.bssid = bssid
    ep.ssid = ssid
    ep.trusted = trusted
    ep.max_in_window = max_in_window([d.ts for d in ep.deviations], params.persist_window_s)
    ep.rule_b = ep.max_in_window >= params.persistence
    peak = max(ep.deviations, key=lambda d: abs(d.dev_db))
    ep.max_dev_db = abs(peak.dev_db)
    ep.direction = "stronger" if peak.dev_db > 0 else "weaker"
    facets = {d.facet for d in ep.deviations}
    ep.facet = "both" if (("median_shift" in facets or "both" in facets)
                          and ("spread_inflation" in facets or "both" in facets)) else facets.pop()
    if ep.rule_b:
        ep.severity = grade_b(ep.max_dev_db, ep.direction, ep.trusted, ep.corroborated_a, params)
    return ep


def classify_unknowns(expanded, tsets, apspoof, params):
    """Pure: turn (device, exact-SSID) rows into Unknown records for the BSSIDs
    that advertise an established SSID but are not in its trusted set. The SSID
    is matched EXACTLY via tsets (keyed on the exact name), so IK-WIFI-DOT1X is
    never checked against IK-WIFI's trusted set. `expanded` rows are dicts with
    the INVENTORY_SQL columns plus a per-exact-SSID `ssid`."""
    out = []
    for a in expanded:
        ts = tsets.get(a["ssid"])
        if ts is None or a["bssid"] in ts["bssids"] or a["trusted"]:
            continue
        if not a["win_obs"]:
            continue
        out.append(Unknown(
            device_key=a["device_key"], bssid=a["bssid"], oui=a["oui"], manuf=a["manuf"],
            ssid=a["ssid"], crypt=a["crypt"], random_bssid=a["random_bssid"],
            first_seen=a["first_seen"], win_first=a["win_first"], win_last=a["win_last"],
            win_obs=a["win_obs"] or 0, win_median=a["win_median"],
            oui_in_trusted=a["oui"] in ts["ouis"],
            crypt_matches=bool(a["crypt"]) and a["crypt"] == ts["dominant_crypt"],
            trusted_bssids=sorted(ts["bssids"]), trusted_ouis=sorted(ts["ouis"]),
            dominant_crypt=ts["dominant_crypt"], group_median=ts["group_median"],
            apspoof_ids=apspoof.get(a["bssid"], [])))
    return out


def assess_unknown(u, params):
    """Fill a signal-(a) record. Sustained + non-random is required to emit."""
    u.sustained = (not u.random_bssid) and (
        u.span_s >= params.persist_hours * 3600 or u.win_obs >= params.persist_obs)
    r = u.win_median if u.win_median is not None else None
    u.close = r is not None and (
        r >= params.close_dbm or (u.group_median is not None and r >= u.group_median + params.strong_db))
    if u.sustained:
        u.severity = grade_a(u.oui_in_trusted, u.crypt_matches, u.close, u.corroborated_b)
    return u


# --- evidence + findings --------------------------------------------------------------

def _num(v, digits=2):
    return None if v is None else round(float(v), digits)


def evidence_b(ep, params):
    b = ep.baseline
    return {
        "detector": TYPE, "version": VERSION,
        "rules": {"rssi_deviation": True, "unknown_bssid": ep.corroborated_a},
        "window_start": iso(ep.first_ts), "window_end": iso(ep.last_ts),
        "duration_s": round(ep.duration_s, 3), "ssid": ep.ssid,
        "ap": {"device_key": ep.device_key, "bssid": ep.bssid, "trusted": ep.trusted},
        "rssi": {
            "facet": ep.facet,
            "baseline": {"median": _num(b.get("rssi_median")),
                         "robust_sd": _num(b.get("rssi_robust_sd")),
                         "n_obs": b.get("base_n_obs"), "n_days": b.get("base_n_days"),
                         "window_start": iso(b.get("base_from")), "window_end": iso(b.get("base_to"))},
            "observed": {"max_dev_db": _num(ep.max_dev_db),
                         "k_sigma": _num(ep.max_dev_db / max(float(b.get("rssi_robust_sd") or 0),
                                                             params.sd_floor_db)),
                         "spread_mad": _num(max(d.window_mad for d in ep.deviations)),
                         "n_windows": len(ep.deviations), "max_in_window": ep.max_in_window,
                         "span_s": round(ep.duration_s, 3), "direction": ep.direction},
            "threshold": {"k": params.k, "floor_db": params.floor_db,
                          "persistence_obs": params.persistence, "window_w": params.window_w},
        },
        "params": params.as_dict(),
    }


def evidence_a(u, params):
    return {
        "detector": TYPE, "version": VERSION,
        "rules": {"rssi_deviation": u.corroborated_b, "unknown_bssid": True},
        "window_start": iso(u.win_first or u.first_seen), "window_end": iso(u.win_last or u.win_first or u.first_seen),
        "duration_s": round(u.span_s, 3), "ssid": u.ssid,
        "ap": {"device_key": u.device_key, "bssid": u.bssid, "oui": u.oui, "manuf": u.manuf,
               "crypt": u.crypt, "trusted": False, "random_bssid": u.random_bssid},
        "membership": {
            "trusted_source": params.trusted_source,
            "trusted_bssids": u.trusted_bssids, "trusted_ouis": u.trusted_ouis,
            "dominant_crypt": u.dominant_crypt, "oui_in_trusted": u.oui_in_trusted,
            "crypt_matches": u.crypt_matches, "first_seen": iso(u.first_seen),
            "apspoof_alert_ids": u.apspoof_ids,
            "persistence": {"n_obs": u.win_obs, "span_s": round(u.span_s, 3), "sustained": u.sustained},
        },
        "observed": {"win_median_dbm": _num(u.win_median), "group_median_dbm": _num(u.group_median),
                     "close": u.close},
        "params": params.as_dict(),
    }


def summary_b(ep):
    return ("Possible evil twin on %s: RSSI %s its %.1f dBm baseline by %.1f dB "
            "(%s) sustained over %s%s" % (
                ep.label(), ep.direction, float(ep.baseline.get("rssi_median") or 0),
                ep.max_dev_db, ep.facet.replace("_", " "), fmt_dur(ep.duration_s),
                "; unknown BSSID for this SSID present" if ep.corroborated_a else ""))


def summary_a(u):
    how = "OUI+crypt match trusted infrastructure" if (u.oui_in_trusted and u.crypt_matches) \
        else ("foreign OUI" if not u.oui_in_trusted else "encryption differs")
    return ("Unknown BSSID %s advertising established SSID %r (%s%s)%s" % (
        u.bssid, u.ssid, how, ", close/strong" if u.close else "",
        "; a trusted BSSID of this SSID is also deviating" if u.corroborated_b else ""))


def make_finding_b(ep, params):
    return detections.Finding(
        type=TYPE, ts=ep.first_ts, severity=ep.severity, device_key=ep.device_key,
        mac=ep.bssid, ssid=ep.ssid, summary=summary_b(ep),
        evidence=evidence_b(ep, params), window_start=ep.first_ts, window_end=ep.last_ts)


def make_finding_a(u, params):
    start = u.win_first or u.first_seen
    end = u.win_last or start
    return detections.Finding(
        type=TYPE, ts=start, severity=u.severity, device_key=u.device_key,
        mac=u.bssid, ssid=u.ssid, summary=summary_a(u),
        evidence=evidence_a(u, params), window_start=start, window_end=end)


# --- database phase -------------------------------------------------------------------------

def _log(msg, quiet=False):
    if not quiet:
        print(msg, file=sys.stderr)


def _trusted_sets(inventory, params, frm):
    """Per exact SSID: the trusted BSSID set, its OUI set, dominant crypt and
    group median RSSI. whitelist = ap_baselines.trusted rows; baseline = BSSIDs
    first seen before --from + --baseline-hours."""
    cutoff = frm + timedelta(hours=params.baseline_hours)
    by_ssid = defaultdict(list)
    for a in inventory:
        by_ssid[a["ssid"]].append(a)
    sets = {}
    for ssid, aps in by_ssid.items():
        if params.trusted_source == "whitelist":
            trusted = [a for a in aps if a["trusted"]]
        else:
            trusted = [a for a in aps if a["first_seen"] < cutoff]
        if not trusted:
            continue
        crypts = [a["crypt"] for a in trusted if a["crypt"]]
        meds = [float(a["win_median"]) for a in trusted if a["win_median"] is not None]
        sets[ssid] = {
            "bssids": {a["bssid"] for a in trusted},
            "ouis": {a["oui"] for a in trusted},
            "dominant_crypt": statistics.mode(crypts) if crypts else None,
            "group_median": statistics.median(meds) if meds else None,
        }
    return sets


def analyse(conn, sensor, frm, to, params, quiet=False):
    """Read phase. Returns (episodes, unknowns, findings, info)."""
    sid = sensor["id"]
    scan_from = frm - timedelta(seconds=params.gap_s)
    scan_to = to + timedelta(seconds=params.gap_s)

    # --- signal (b): windowed deviations of every baselined AP -------------------
    _log("scanning RSSI %s .. %s ..." % (fmt_ts(scan_from), fmt_ts(scan_to)), quiet)
    rows = conn.execute(OBS_SQL, {"sid": sid, "scan_from": scan_from, "scan_to": scan_to}).fetchall()
    per_ap = defaultdict(list)
    ap_meta = {}
    for r in rows:
        per_ap[r["device_key"]].append((r["ts"], r["rssi"]))
        if r["device_key"] not in ap_meta:
            ap_meta[r["device_key"]] = r
    deviations = []
    for key, readings in per_ap.items():
        m = ap_meta[key]
        deviations += windowed_deviations(readings, float(m["rssi_median"]),
                                          float(m["rssi_robust_sd"]), params, key)
    episodes = [e for e in build_episodes(deviations, params.gap_s) if e.overlaps(frm, to)]

    # --- signal (a): unknown BSSIDs for established SSIDs -------------------------
    inv = conn.execute(INVENTORY_SQL, {"sid": sid, "scan_from": scan_from, "scan_to": scan_to}).fetchall()
    inv = [dict(r) for r in inv]
    history = defaultdict(set)
    for r in conn.execute(HISTORY_SQL, {"sid": sid}):
        history[r["device_key"]].add(r["ssid"])
    # expand each AP to every exact SSID it has ever advertised
    expanded = []
    for a in inv:
        ssids = {a["ssid"]} | history.get(a["device_key"], set())
        for s in ssids:
            expanded.append(dict(a, ssid=s))
    tsets = _trusted_sets(expanded, params, frm)
    apspoof = defaultdict(list)
    for r in conn.execute(ALERTS_SQL, {"sid": sid, "headers": list(APSPOOF_HEADERS),
                                       "scan_from": scan_from, "scan_to": scan_to}):
        apspoof[r["bssid"]].append(r["id"])

    unknowns = classify_unknowns(expanded, tsets, apspoof, params)

    # --- corroboration: an unknown BSSID and a deviating trusted member, same SSID
    dev_ssids = {e.device_key: ap_meta[e.device_key]["ssid"] for e in episodes}
    ssid_has_unknown = {u.ssid for u in unknowns}
    ssid_has_deviation = set(dev_ssids.values())
    for e in episodes:
        e.corroborated_a = ap_meta[e.device_key]["ssid"] in ssid_has_unknown
    for u in unknowns:
        u.corroborated_b = u.ssid in ssid_has_deviation

    # --- assess + build findings -------------------------------------------------
    findings = []
    for e in episodes:
        m = ap_meta[e.device_key]
        assess(e, params, m, m["bssid"], m["ssid"], m["trusted"])
        if e.emitted:
            e.finding = make_finding_b(e, params)
            findings.append(e.finding)
    for u in unknowns:
        assess_unknown(u, params)
        if u.sustained:
            u.finding = make_finding_a(u, params)
            findings.append(u.finding)

    info = {
        "scan_from": scan_from, "scan_to": scan_to,
        "aps_scanned": len(per_ap), "deviations": len(deviations),
        "episodes": len(episodes), "established_ssids": len(tsets),
        "unknown_candidates": len(unknowns),
        "findings_b": sum(1 for e in episodes if e.finding),
        "findings_a": sum(1 for u in unknowns if u.finding),
    }
    return episodes, unknowns, findings, info


# --- CLI -------------------------------------------------------------------------------------

def add_arguments(p):
    p.add_argument("--from", dest="from_", metavar="WHEN",
                   help="window start, ISO 8601 (UTC if no offset); default: --to minus 24h")
    p.add_argument("--to", metavar="WHEN", help="window end (exclusive); default: now")
    p.add_argument("--sensor", metavar="NAME", help="sensors.name (default: the first sensor)")
    p.add_argument("--gap", type=float, default=300, metavar="S",
                   help="silence that closes an episode (default 300)")
    p.add_argument("--window", "-W", type=int, default=10, metavar="N",
                   help="observations per sliding window for signal b (default 10)")
    p.add_argument("--k", type=float, default=6.0,
                   help="median-shift threshold in robust sigmas (default 6)")
    p.add_argument("--floor-db", type=float, default=8.0, metavar="DB",
                   help="absolute floor on the median shift, dB (default 8)")
    p.add_argument("--spread-k", type=float, default=3.0,
                   help="spread-inflation: window MAD vs k*baseline sigma (default 3)")
    p.add_argument("--persistence", type=int, default=6, metavar="N",
                   help="deviating observations required within --persist-window (default 6)")
    p.add_argument("--persist-window", type=float, default=900, metavar="S",
                   help="sliding window for the persistence rule (default 900)")
    p.add_argument("--med-dev", type=float, default=12.0, metavar="DB",
                   help="deviation that grades an episode 'medium' (default 12)")
    p.add_argument("--high-dev", type=float, default=20.0, metavar="DB",
                   help="deviation that grades an episode 'high' (default 20)")
    p.add_argument("--trusted-source", choices=("whitelist", "baseline"), default="whitelist",
                   help="trusted BSSID set: operator whitelist, or baseline-window first-seen "
                        "(for the seeded FP audit) (default whitelist)")
    p.add_argument("--baseline-hours", type=float, default=24, metavar="H",
                   help="baseline-source cutoff = --from + this (default 24)")
    p.add_argument("--persist-hours", type=float, default=1.0, metavar="H",
                   help="signal a: sustained if present this long ... (default 1)")
    p.add_argument("--persist-obs", type=int, default=20, metavar="N",
                   help="... or this many in-window observations (default 20)")
    p.add_argument("--dry-run", action="store_true", help="analyse and print, write nothing")
    p.add_argument("--verbose", "-v", action="store_true", help="print every episode/candidate")
    p.add_argument("--json", action="store_true",
                   help="print the findings as JSON on stdout (report goes to stderr)")


def run(args):
    params = Params.from_args(args)
    try:
        frm, to = common.window_from_args(args.from_, args.to)
    except ValueError as e:
        sys.exit(str(e))
    conn = common.connect()
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            sensor = common.get_sensor(conn, args.sensor)
            episodes, unknowns, findings, info = analyse(conn, sensor, frm, to, params, quiet=args.json)
        if not args.dry_run and findings:
            with conn.transaction():
                for f in findings:
                    detections.write(conn, sensor["id"], f, params.gap_s)
    finally:
        conn.close()
    report(sensor, frm, to, params, episodes, unknowns, findings, info, args)
    return 0


def report(sensor, frm, to, params, episodes, unknowns, findings, info, args):
    out = sys.stderr if args.json else sys.stdout
    print("evil_twin  sensor %s (id %d)  window %s .. %s UTC%s" % (
        sensor["name"], sensor["id"], fmt_ts(frm), fmt_ts(to),
        "  [dry run]" if args.dry_run else ""), file=out)
    print("  params: window %d, k %g, floor %g dB, persistence %d in %ss, gap %ss, trusted-source %s"
          % (params.window_w, params.k, params.floor_db, params.persistence,
             params.persist_window_s, params.gap_s, params.trusted_source), file=out)
    print("  scanned %s .. %s: %d APs, %d deviating windows, %d episodes -> %d (b) detections; "
          "%d established SSIDs, %d unknown-BSSID candidates -> %d (a) detections"
          % (fmt_ts(info["scan_from"]), fmt_ts(info["scan_to"]), info["aps_scanned"],
             info["deviations"], info["episodes"], info["findings_b"], info["established_ssids"],
             info["unknown_candidates"], info["findings_a"]), file=out)
    print(file=out)
    rows = []
    for i, e in enumerate(e for e in episodes if e.finding):
        rows.append((i + 1, "b", (e.ssid or "<hidden>")[:20], e.bssid, fmt_ts(e.first_ts),
                     fmt_dur(e.duration_s), "%.1f" % e.max_dev_db, e.direction, e.facet[:6],
                     "%d/%d" % (e.max_in_window, params.persistence), e.severity,
                     "%s #%d" % (e.finding.action, e.finding.id) if e.finding.action else
                     "would write" if args.dry_run else "-"))
    for i, u in enumerate(u for u in unknowns if u.finding):
        rows.append((len(rows) + 1, "a", (u.ssid or "?")[:20], u.bssid, fmt_ts(u.win_first or u.first_seen),
                     fmt_dur(u.span_s), "-", "-", "oui" if not u.oui_in_trusted else "crypt" if not u.crypt_matches else "match",
                     "%d obs" % u.win_obs, u.severity,
                     "%s #%d" % (u.finding.action, u.finding.id) if u.finding.action else
                     "would write" if args.dry_run else "-"))
    common.table(["#", "sig", "ssid", "bssid", "start (UTC)", "dur", "dev dB", "dir", "facet",
                  "persist", "sev", "action"], rows, align_right={0, 5, 6}, out=out)
    if args.verbose:
        _verbose(episodes, unknowns, params, out)
    if args.json:
        print(json.dumps([{
            "id": f.id, "action": f.action, "ts": iso(f.ts), "type": f.type, "severity": f.severity,
            "device_key": f.device_key, "mac": f.mac, "ssid": f.ssid, "summary": f.summary,
            "evidence": f.evidence} for f in findings], indent=2, default=str))


def _verbose(episodes, unknowns, params, out):
    print(file=out)
    print("signal (b) episodes:", file=out)
    for e in episodes:
        status = "-> %s" % e.severity if e.emitted else "below persistence (%d/%d)" % (
            e.max_in_window, params.persistence)
        print("  %s  %s .. %s  max %.1f dB %s %s" % (
            e.label(), fmt_ts(e.first_ts), fmt_ts(e.last_ts).split(" ")[1], e.max_dev_db,
            e.direction or "", status), file=out)
    print("signal (a) candidates:", file=out)
    for u in unknowns:
        print("  %s %r  %d obs over %s  oui_in_trusted=%s crypt_matches=%s -> %s" % (
            u.bssid, u.ssid, u.win_obs, fmt_dur(u.span_s), u.oui_in_trusted, u.crypt_matches,
            u.severity or "not sustained"), file=out)
