"""deauth_flood - deauth/disassoc flood episodes per access point.

Two signals, both per sensor and per AP, read over the window [--from, --to)
widened by the episode gap on both sides:

  1. Kismet DEAUTHFLOOD alerts (primary trigger). Kismet raises one when more
     than 10 deauth/disassoc frames for a BSSID arrive with no gap > 1 s between
     consecutive frames, throttled by `alert=DEAUTHFLOOD,5/min,2/sec`. The AP is
     joined by alerts.transmitter_mac (= the BSSID); alerts.device_key is the
     useless '00_0'. BCASTDISCON and DISCONCODEINVALID alerts corroborate an
     episode but never start one.

  2. Burst events from observations.disconnects. That column is Kismet's
     dot11.device.client_disconnects, which is NOT a cumulative counter
     (phy_80211.cc):

         if (now - client_disconnects_last > 1) client_disconnects = 1;
         else                                   client_disconnects += 1;
         if (client_disconnects > 10) { raise DEAUTHFLOOD; client_disconnects = 1; }

     It is the size of the current burst, never above 11, restarting at 1 after
     a pause and after every alert, so during a flood its polled value is
     essentially random in 1..11 and deltas mean nothing. What a poll does tell:
     a value that CHANGED to a non-zero number since the previous poll means at
     least one new burst happened in between (a drop to 0 is a device
     re-creation after a Kismet restart). That change is a "burst event" - a
     lower bound on bursts, but organically rare and isolated (seeded data:
     76 events on 28 of 343 APs in 5 days, never two consecutive polls, never
     more than 2 per AP per hour), while a flood changes it in ~10 of 11 polls.

Alerts and burst events of one AP are merged into episodes (silence > --gap
closes one). An episode is a detection when

  A. it holds >= 1 DEAUTHFLOOD alert, or
  B. it holds >= T burst events within a sliding --burst-window, where
     T = max(--min-bursts, Poisson quantile at --alpha of the AP's own organic
     burst rate learned over --lookback) - the per-AP baseline.

Severity: A with alert span < 2 s (one burst-second; what organic client storms
look like) -> low; A spanning 2 s..--sustain, or B alone -> medium; A spanning
>= --sustain (alerts in two throttle minutes) or A and B together -> high;
one level up when the frames come from the AP side (from_ap/broadcast) on an
operator-trusted AP (ap_baselines.trusted).

Evidence only (not a trigger, to be judged against a staged attack): the AP's
non-data frame rate per poll, d(pk_total - pk_data), with a median + 1.4826*MAD
baseline over the lookback, the same estimator refresh_ap_baselines() uses.
"""

import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import common, detections
from .common import POLL_INTERVAL_S, fmt_dur, fmt_ts, iso

TYPE = "deauth_flood"
VERSION = 1
TRIGGER_HEADERS = ("DEAUTHFLOOD",)
CORROBORATING_HEADERS = ("BCASTDISCON", "DISCONCODEINVALID")
ATTACK_DIRECTIONS = ("from_ap", "broadcast")
SEVERITIES = ("info", "low", "medium", "high", "critical")
BASELINE_MIN_HOURS = 24         # less lookback data than this -> fall back to the scan range


# --- SQL ----------------------------------------------------------------------------
# Every statement here is a SELECT; the write goes through detections.write().

_EVENTS_CTE = """
WITH s AS (
  SELECT o.device_key, o.ts,
         o.disconnects                                                   AS cur,
         lag(o.disconnects) OVER w                                       AS prev,
         o.ts - lag(o.ts) OVER w                                         AS since_prev,
         (o.pk_total - o.pk_data) - lag(o.pk_total - o.pk_data) OVER w   AS mgmt_delta
  FROM observations o
  JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
  WHERE o.sensor_id = %(sid)s AND d.type = 'ap'
    AND o.ts >= %(scan_from)s AND o.ts < %(scan_to)s{key_filter}
  WINDOW w AS (PARTITION BY o.device_key ORDER BY o.ts)
)"""
_KEY_FILTER = "\n    AND o.device_key = ANY(%(keys)s)"
_BURST_COND = """prev IS NOT NULL AND cur IS NOT NULL AND cur <> prev AND cur > 0
      AND since_prev <= %(max_gap_s)s * interval '1 second'"""
_REGULAR_POLL = "since_prev BETWEEN interval '25 seconds' AND interval '35 seconds'"

# Burst events of every AP in the scan range (one pass over observations).
EVENTS_SQL = _EVENTS_CTE.format(key_filter="") + """
SELECT device_key, ts, prev, cur, extract(epoch FROM since_prev)::int AS since_prev_s, mgmt_delta
FROM s
WHERE """ + _BURST_COND + """
ORDER BY device_key, ts"""

# Flood alerts with the AP resolved by BSSID and the frame direction.
ALERTS_SQL = """
SELECT a.id, a.ts, a.header,
       upper(a.transmitter_mac::text) AS bssid,
       upper(a.source_mac::text) AS source_mac, upper(a.dest_mac::text) AS dest_mac,
       d.device_key, d.ssid,
       CASE WHEN a.dest_mac = macaddr 'ff:ff:ff:ff:ff:ff' THEN 'broadcast'
            WHEN a.source_mac = a.transmitter_mac        THEN 'from_ap'
            WHEN a.dest_mac   = a.transmitter_mac        THEN 'to_ap'
            ELSE 'other' END AS direction
FROM alerts a
LEFT JOIN LATERAL (
  SELECT device_key, ssid FROM devices d
  WHERE d.sensor_id = a.sensor_id AND d.mac = a.transmitter_mac AND d.type = 'ap'
  ORDER BY last_seen DESC LIMIT 1) d ON true
WHERE a.sensor_id = %(sid)s AND a.header = ANY(%(headers)s)
  AND a.ts IS NOT NULL AND a.transmitter_mac IS NOT NULL
  AND a.ts >= %(scan_from)s AND a.ts < %(scan_to)s
ORDER BY a.transmitter_mac, a.ts"""

# Per-AP baseline over a range, for the APs that have an episode only (indexed
# by observations_device_ts_rssi on (sensor_id, device_key, ts)).
BASELINE_SQL = _EVENTS_CTE.format(key_filter=_KEY_FILTER) + """, stats AS (
  SELECT device_key,
         count(*)                                       AS polls,
         count(*) FILTER (WHERE """ + _BURST_COND + """) AS bursts,
         min(ts) AS first_ts, max(ts) AS last_ts,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY mgmt_delta)
           FILTER (WHERE mgmt_delta >= 0 AND """ + _REGULAR_POLL + """) AS mgmt_med
  FROM s GROUP BY device_key
), mad AS (
  SELECT s.device_key,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(s.mgmt_delta - st.mgmt_med)) AS mgmt_mad
  FROM s JOIN stats st USING (device_key)
  WHERE s.mgmt_delta >= 0 AND """ + _REGULAR_POLL + """
  GROUP BY s.device_key
)
SELECT st.device_key, st.polls, st.bursts, st.first_ts, st.last_ts,
       st.polls * %(poll_s)s / 3600.0 AS active_hours,
       st.mgmt_med, 1.4826 * m.mgmt_mad AS mgmt_robust_sd
FROM stats st LEFT JOIN mad m USING (device_key)"""

# Trigger alerts per BSSID over the baseline range (history of organic floods).
BASELINE_ALERTS_SQL = """
SELECT upper(transmitter_mac::text) AS bssid, count(*) AS n
FROM alerts
WHERE sensor_id = %(sid)s AND header = ANY(%(headers)s) AND transmitter_mac IS NOT NULL
  AND ts >= %(from)s AND ts < %(to)s AND upper(transmitter_mac::text) = ANY(%(bssids)s)
GROUP BY 1"""

# The polls of one AP around an episode (non-data frame rate during the episode).
EPISODE_POLLS_SQL = _EVENTS_CTE.format(key_filter=_KEY_FILTER) + """
SELECT ts, prev, cur, extract(epoch FROM since_prev)::int AS since_prev_s, mgmt_delta
FROM s WHERE """ + _REGULAR_POLL + """ AND mgmt_delta >= 0
ORDER BY ts"""

AP_INFO_SQL = """
SELECT device_key, upper(bssid::text) AS bssid, ssid, crypt, mfp_req,
       coalesce(trusted, false) AS trusted
FROM ap_inventory WHERE sensor_id = %(sid)s AND device_key = ANY(%(keys)s)"""


# --- data ---------------------------------------------------------------------------

@dataclass
class Params:
    gap_s: float = 300
    max_gap_s: float = 120
    min_bursts: int = 3
    burst_window_s: float = 600
    alpha: float = 1e-4
    sustain_s: float = 60
    lookback_s: float = 7 * 86400
    headers: tuple = TRIGGER_HEADERS

    @classmethod
    def from_args(cls, args):
        return cls(gap_s=args.gap, max_gap_s=args.max_gap, min_bursts=args.min_bursts,
                   burst_window_s=args.burst_window, alpha=args.alpha, sustain_s=args.sustain,
                   lookback_s=common.parse_duration(args.lookback),
                   headers=tuple(h.strip().upper() for h in args.headers.split(",") if h.strip()))

    def as_dict(self):
        return {"gap_s": self.gap_s, "max_gap_s": self.max_gap_s, "min_bursts": self.min_bursts,
                "burst_window_s": self.burst_window_s, "alpha": self.alpha,
                "sustain_s": self.sustain_s, "lookback_s": self.lookback_s,
                "trigger_headers": list(self.headers)}


@dataclass
class Alert:
    id: int
    ts: datetime
    header: str
    bssid: str
    source_mac: str | None
    dest_mac: str | None
    direction: str
    device_key: str | None = None
    ssid: str | None = None


@dataclass
class Burst:
    ts: datetime
    device_key: str
    prev: int
    cur: int
    since_prev_s: int
    mgmt_delta: int | None = None


@dataclass
class Episode:
    subject: str                        # device_key, or the BSSID when no device row exists
    alerts: list = field(default_factory=list)          # trigger alerts
    corroborating: list = field(default_factory=list)   # other flood-related alerts
    bursts: list = field(default_factory=list)
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    # filled by assess()
    device_key: str | None = None
    bssid: str | None = None
    ssid: str | None = None
    ap: dict | None = None
    baseline: dict | None = None
    threshold: int | None = None
    max_in_window: int = 0
    rule_a: bool = False
    rule_b: bool = False
    severity: str | None = None
    finding: detections.Finding | None = None

    def add(self, ts, obj, trigger_headers):
        if isinstance(obj, Alert):
            (self.alerts if obj.header in trigger_headers else self.corroborating).append(obj)
            self.bssid = self.bssid or obj.bssid
            self.device_key = self.device_key or obj.device_key
            self.ssid = self.ssid if self.ssid is not None else obj.ssid
        else:
            self.bursts.append(obj)
            self.device_key = self.device_key or obj.device_key
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    @property
    def duration_s(self):
        return (self.last_ts - self.first_ts).total_seconds()

    @property
    def alert_span_s(self):
        if not self.alerts:
            return None
        return (max(a.ts for a in self.alerts) - min(a.ts for a in self.alerts)).total_seconds()

    @property
    def directions(self):
        return sorted({a.direction for a in self.alerts})

    @property
    def sources(self):
        return sorted({a.source_mac for a in self.alerts if a.source_mac})

    @property
    def broadcast(self):
        return ("broadcast" in self.directions
                or any(a.header == "BCASTDISCON" for a in self.corroborating))

    @property
    def attack_like(self):
        """Frames came from the AP side: what a spoofing attacker produces, and
        what an organic client storm does not."""
        return self.broadcast or any(d in ATTACK_DIRECTIONS for d in self.directions)

    @property
    def emitted(self):
        return self.rule_a or self.rule_b

    def overlaps(self, frm, to):
        return self.last_ts >= frm and self.first_ts < to

    def label(self):
        return "%s (%s)" % (self.ssid if self.ssid else ("<hidden>" if self.ssid == "" else "<unknown AP>"),
                            self.bssid or self.subject)


# --- pure logic (unit-tested without a database) ----------------------------------------

def build_episodes(alerts, bursts, gap_s, trigger_headers=TRIGGER_HEADERS):
    """Merge alerts and burst events per AP into episodes; a silence longer
    than gap_s between consecutive events closes one."""
    by_subject = defaultdict(list)
    for a in alerts:
        by_subject[a.device_key or a.bssid].append((a.ts, a))
    for b in bursts:
        by_subject[b.device_key].append((b.ts, b))
    episodes = []
    for subject, events in by_subject.items():
        events.sort(key=lambda e: e[0])
        current = None
        for ts, obj in events:
            if current is None or (ts - current.last_ts).total_seconds() > gap_s:
                current = Episode(subject=subject)
                episodes.append(current)
            current.add(ts, obj, trigger_headers)
    episodes.sort(key=lambda e: (e.first_ts, e.subject))
    return episodes


def max_in_window(times, window_s):
    """Largest number of the (sorted) timestamps that fit in a window of
    window_s seconds (inclusive at both ends)."""
    times = sorted(times)
    best = j = 0
    for i, t in enumerate(times):
        while (t - times[j]).total_seconds() > window_s:
            j += 1
        best = max(best, i - j + 1)
    return best


def poisson_threshold(rate_per_hour, window_s, alpha, floor):
    """Smallest count x with P(Poisson(rate * window) >= x) < alpha, not below
    floor: the per-AP burst threshold for one window."""
    mu = float(rate_per_hour) * window_s / 3600.0
    if mu <= 0:
        return floor
    p = math.exp(-mu)
    cdf = p
    x = 0
    while cdf <= 1 - alpha and x < 10000:
        x += 1
        p *= mu / x
        cdf += p
    return max(floor, x + 1)


def grade(rule_a, rule_b, alert_span_s, sustain_s, attack_like=False, trusted=False):
    """Severity per the table in the module docstring."""
    if not (rule_a or rule_b):
        return None
    if rule_a and rule_b:
        level = "high"
    elif rule_a:
        if alert_span_s >= sustain_s:
            level = "high"
        elif alert_span_s >= 2:
            level = "medium"
        else:
            level = "low"
    else:
        level = "medium"
    if attack_like and trusted:
        level = SEVERITIES[min(SEVERITIES.index(level) + 1, len(SEVERITIES) - 1)]
    return level


def assess(ep, params, baseline=None, ap=None):
    """Apply the rules to one episode. baseline: row of BASELINE_SQL (or None);
    ap: row of AP_INFO_SQL (or None). Fills the assessment fields of ep."""
    ep.ap = ap
    ep.baseline = baseline
    if ap:
        ep.bssid = ap["bssid"]
        ep.ssid = ap["ssid"]
    rate = 0.0
    if baseline and baseline.get("active_hours"):
        rate = float(baseline["bursts"]) / float(baseline["active_hours"])
    ep.threshold = poisson_threshold(rate, params.burst_window_s, params.alpha, params.min_bursts)
    ep.max_in_window = max_in_window([b.ts for b in ep.bursts], params.burst_window_s)
    ep.rule_a = any(a.header in params.headers for a in ep.alerts)
    ep.rule_b = ep.max_in_window >= ep.threshold
    ep.severity = grade(ep.rule_a, ep.rule_b, ep.alert_span_s, params.sustain_s,
                        attack_like=ep.attack_like, trusted=bool(ap and ap.get("trusted")))
    return ep


def summary_text(ep, params):
    label = ep.label()
    if ep.rule_a:
        n = len(ep.alerts)
        span = ep.alert_span_s
        srcs = ep.sources
        dirs = ", ".join(d.replace("_", " ") for d in ep.directions) or "direction unknown"
        parts = ["%d DEAUTHFLOOD alert%s within %s" % (n, "" if n == 1 else "s", fmt_dur(span))]
        if srcs:
            parts[-1] += " from %d source%s (%s)" % (len(srcs), "" if len(srcs) == 1 else "s", dirs)
        parts.append("%d burst poll%s" % (len(ep.bursts), "" if len(ep.bursts) == 1 else "s"))
        if ep.corroborating:
            parts.append("+ " + ", ".join(sorted({a.header for a in ep.corroborating})))
        if span >= params.sustain_s:
            tail = "sustained for %s" % fmt_dur(ep.duration_s)
        elif ep.rule_b:
            tail = "burst activity above the AP's baseline"
        elif span >= 2:
            tail = "several seconds"
        else:
            tail = "brief"
        return "Deauth/disassoc flood on %s: %s; %s" % (label, ", ".join(parts), tail)
    return ("Sustained deauth/disassoc activity on %s: %d burst polls within %s "
            "(threshold %d, baseline %.3f/h), no Kismet flood alert"
            % (label, ep.max_in_window, fmt_dur(params.burst_window_s), ep.threshold,
               _rate(ep.baseline) or 0.0))


def _rate(baseline):
    if not baseline or not baseline.get("active_hours"):
        return None
    return float(baseline["bursts"]) / float(baseline["active_hours"])


def evidence_dict(ep, params, observed=None, baseline_range=None):
    by_header = defaultdict(int)
    for a in ep.alerts:
        by_header[a.header] += 1
    corr = {h: [a.id for a in ep.corroborating if a.header == h] for h in CORROBORATING_HEADERS}
    dur = ep.duration_s
    active = max(dur, POLL_INTERVAL_S)
    bursts_per_hour = len(ep.bursts) * 3600.0 / active if ep.bursts else 0.0
    b = ep.baseline or {}
    base_rate = _rate(b)
    ev = {
        "detector": TYPE, "version": VERSION,
        "window_start": iso(ep.first_ts), "window_end": iso(ep.last_ts), "duration_s": round(dur, 3),
        "rules": {"alerts": ep.rule_a, "bursts": ep.rule_b},
        "alerts": {
            "ids": [a.id for a in ep.alerts], "n": len(ep.alerts),
            "span_s": round(ep.alert_span_s, 3) if ep.alerts else None,
            "first_ts": iso(min(a.ts for a in ep.alerts)) if ep.alerts else None,
            "last_ts": iso(max(a.ts for a in ep.alerts)) if ep.alerts else None,
            "by_header": dict(by_header),
            "sources": ep.sources, "directions": ep.directions, "broadcast": ep.broadcast,
            "corroborating": corr,
        },
        "bursts": {
            "n": len(ep.bursts), "polls": [iso(x.ts) for x in ep.bursts],
            "values": [[x.prev, x.cur] for x in ep.bursts],
            "max_in_window": ep.max_in_window, "threshold": ep.threshold,
        },
        "baseline": {
            "from": iso(baseline_range[0]) if baseline_range else None,
            "to": iso(baseline_range[1]) if baseline_range else None,
            "fallback": bool(b.get("fallback")) if b else None,
            "active_hours": round(float(b["active_hours"]), 2) if b.get("active_hours") is not None else None,
            "polls": b.get("polls"), "bursts": b.get("bursts"),
            "bursts_per_hour": round(base_rate, 4) if base_rate is not None else None,
            "flood_alerts": b.get("flood_alerts"),
            "mgmt_per_poll_median": _num(b.get("mgmt_med")),
            "mgmt_per_poll_robust_sd": _num(b.get("mgmt_robust_sd")),
        },
        "observed": {
            "bursts_per_hour": round(bursts_per_hour, 3),
            "ratio_to_baseline": (round(bursts_per_hour / base_rate, 1)
                                  if base_rate and ep.bursts else None),
            "mgmt_per_poll_max": _num((observed or {}).get("mgmt_max")),
            "mgmt_per_poll_mean": _num((observed or {}).get("mgmt_mean")),
            "mgmt_ratio_to_median": _num((observed or {}).get("mgmt_ratio")),
            "polls": (observed or {}).get("polls"),
        },
        "ap": {
            "bssid": ep.bssid, "ssid": ep.ssid,
            "crypt": (ep.ap or {}).get("crypt"), "mfp_req": (ep.ap or {}).get("mfp_req"),
            "trusted": bool((ep.ap or {}).get("trusted")), "device_key": ep.device_key,
        },
        "params": params.as_dict(),
    }
    return ev


def _num(v, digits=2):
    return None if v is None else round(float(v), digits)


def make_finding(ep, params, observed=None, baseline_range=None):
    return detections.Finding(
        type=TYPE, ts=ep.first_ts, severity=ep.severity, device_key=ep.device_key,
        mac=ep.bssid, ssid=ep.ssid, summary=summary_text(ep, params),
        evidence=evidence_dict(ep, params, observed, baseline_range),
        window_start=ep.first_ts, window_end=ep.last_ts)


# --- database phase -------------------------------------------------------------------------

def _log(msg, quiet=False):
    if not quiet:
        print(msg, file=sys.stderr)


def analyse(conn, sensor, frm, to, params, quiet=False):
    """Read phase. Returns (episodes, findings, info): every episode in the scan
    range (assessed when it overlaps [frm, to)), the Finding of each emitted
    one, and run statistics for the report."""
    sid = sensor["id"]
    scan_from = frm - timedelta(seconds=params.gap_s)
    scan_to = to + timedelta(seconds=params.gap_s)
    base_from = frm - timedelta(seconds=params.lookback_s)
    headers = list(params.headers) + list(CORROBORATING_HEADERS)

    _log("scanning observations %s .. %s ..." % (fmt_ts(scan_from), fmt_ts(scan_to)), quiet)
    bursts = [Burst(**r) for r in conn.execute(EVENTS_SQL, {
        "sid": sid, "scan_from": scan_from, "scan_to": scan_to,
        "max_gap_s": params.max_gap_s}).fetchall()]
    alerts = [Alert(**r) for r in conn.execute(ALERTS_SQL, {
        "sid": sid, "headers": headers, "scan_from": scan_from, "scan_to": scan_to}).fetchall()]
    episodes = build_episodes(alerts, bursts, params.gap_s, params.headers)
    info = {
        "scan_from": scan_from, "scan_to": scan_to, "baseline_from": base_from, "baseline_to": frm,
        "bursts": len(bursts), "aps_with_bursts": len({b.device_key for b in bursts}),
        "alerts": sum(1 for a in alerts if a.header in params.headers),
        "corroborating": sum(1 for a in alerts if a.header not in params.headers),
        "episodes": len(episodes),
    }

    # Candidates: in the window, and either alerted or with enough bursts for the floor.
    candidates = [ep for ep in episodes if ep.overlaps(frm, to)
                  and (any(a.header in params.headers for a in ep.alerts)
                       or max_in_window([b.ts for b in ep.bursts], params.burst_window_s)
                       >= params.min_bursts)]
    keys = sorted({ep.device_key for ep in candidates if ep.device_key})
    ap_info = {}
    baselines = {}
    if keys:
        ap_info = {r["device_key"]: r for r in conn.execute(AP_INFO_SQL, {"sid": sid, "keys": keys})}
        _log("baseline for %d AP(s) over %s .. %s ..." % (len(keys), fmt_ts(base_from), fmt_ts(frm)), quiet)
        baselines = _baselines(conn, sid, keys, base_from, frm, params)
        short = [k for k in keys
                 if float((baselines.get(k) or {}).get("active_hours") or 0) < BASELINE_MIN_HOURS]
        if short:
            _log("  %d AP(s) have < %d h of lookback: baseline from the scan range instead"
                 % (len(short), BASELINE_MIN_HOURS), quiet)
            for k, row in _baselines(conn, sid, short, base_from, scan_to, params).items():
                row["fallback"] = True
                baselines[k] = row
        # organic flood alerts of these APs in the baseline range
        bssids = sorted({ap_info[k]["bssid"] for k in keys if k in ap_info})
        if bssids:
            hist = {r["bssid"]: r["n"] for r in conn.execute(BASELINE_ALERTS_SQL, {
                "sid": sid, "headers": list(params.headers), "from": base_from, "to": frm,
                "bssids": bssids})}
            for k in keys:
                if k in ap_info and k in baselines:
                    baselines[k]["flood_alerts"] = hist.get(ap_info[k]["bssid"], 0)

    findings = []
    for ep in candidates:
        assess(ep, params, baselines.get(ep.device_key), ap_info.get(ep.device_key))
        if not ep.emitted:
            continue
        observed = _episode_polls(conn, sid, ep, params) if ep.device_key else None
        used = (base_from, scan_to) if (ep.baseline or {}).get("fallback") else (base_from, frm)
        ep.finding = make_finding(ep, params, observed, used)
        findings.append(ep.finding)
    info["candidates"] = len(candidates)
    info["findings"] = len(findings)
    return episodes, findings, info


def _baselines(conn, sid, keys, b_from, b_to, params):
    rows = conn.execute(BASELINE_SQL, {
        "sid": sid, "keys": keys, "scan_from": b_from, "scan_to": b_to,
        "max_gap_s": params.max_gap_s, "poll_s": POLL_INTERVAL_S}).fetchall()
    return {r["device_key"]: dict(r) for r in rows}


def _episode_polls(conn, sid, ep, params):
    """Non-data frame rate of the AP during the episode (regular polls only)."""
    lead = timedelta(seconds=3 * POLL_INTERVAL_S)
    rows = conn.execute(EPISODE_POLLS_SQL, {
        "sid": sid, "keys": [ep.device_key],
        "scan_from": ep.first_ts - lead, "scan_to": ep.last_ts + lead,
        "max_gap_s": params.max_gap_s}).fetchall()
    # keep the polls whose interval touches the episode
    vals = [float(r["mgmt_delta"]) for r in rows
            if r["mgmt_delta"] is not None
            and r["ts"] >= ep.first_ts and r["ts"] - timedelta(seconds=r["since_prev_s"]) <= ep.last_ts]
    if not vals:
        return None
    med = (ep.baseline or {}).get("mgmt_med")
    return {"polls": len(vals), "mgmt_max": max(vals), "mgmt_mean": sum(vals) / len(vals),
            "mgmt_ratio": (max(vals) / float(med)) if med else None}


# --- CLI -------------------------------------------------------------------------------------

def add_arguments(p):
    p.add_argument("--from", dest="from_", metavar="WHEN",
                   help="window start, ISO 8601 (UTC if no offset); default: --to minus 24h")
    p.add_argument("--to", metavar="WHEN", help="window end (exclusive); default: now")
    p.add_argument("--sensor", metavar="NAME", help="sensors.name (default: the first sensor)")
    p.add_argument("--gap", type=float, default=300, metavar="S",
                   help="silence that closes an episode (default 300)")
    p.add_argument("--max-gap", type=float, default=120, metavar="S",
                   help="a counter change counts only if the previous poll is this close (default 120)")
    p.add_argument("--min-bursts", type=int, default=3, metavar="N",
                   help="rule B floor: burst polls within --burst-window (default 3)")
    p.add_argument("--burst-window", type=float, default=600, metavar="S",
                   help="sliding window for rule B (default 600)")
    p.add_argument("--alpha", type=float, default=1e-4,
                   help="Poisson tail for the per-AP threshold above the floor (default 1e-4)")
    p.add_argument("--sustain", type=float, default=60, metavar="S",
                   help="alert span that makes an episode 'high' (default 60)")
    p.add_argument("--lookback", default="7d", metavar="DUR",
                   help="baseline window before --from, e.g. 7d, 36h (default 7d)")
    p.add_argument("--headers", default=",".join(TRIGGER_HEADERS),
                   help="trigger alert headers (default %s)" % ",".join(TRIGGER_HEADERS))
    p.add_argument("--dry-run", action="store_true", help="analyse and print, write nothing")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print every episode's event timeline and baseline")
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
            conn.execute("SET TRANSACTION READ ONLY")      # the analysis can only read
            sensor = common.get_sensor(conn, args.sensor)
            episodes, findings, info = analyse(conn, sensor, frm, to, params, quiet=args.json)
        if not args.dry_run and findings:
            with conn.transaction():                       # the only writes: detections
                for f in findings:
                    detections.write(conn, sensor["id"], f, params.gap_s)
    finally:
        conn.close()
    report(sensor, frm, to, params, episodes, findings, info, args)
    return 0


def report(sensor, frm, to, params, episodes, findings, info, args):
    out = sys.stderr if args.json else sys.stdout
    print("deauth_flood  sensor %s (id %d)  window %s .. %s UTC%s" % (
        sensor["name"], sensor["id"], fmt_ts(frm), fmt_ts(to),
        "  [dry run]" if args.dry_run else ""), file=out)
    print("  params: gap %ss, max-gap %ss, min-bursts %d in %ss, alpha %g, sustain %ss, lookback %s, triggers %s"
          % (params.gap_s, params.max_gap_s, params.min_bursts, params.burst_window_s, params.alpha,
             params.sustain_s, fmt_dur(params.lookback_s), ",".join(params.headers)), file=out)
    print("  scanned %s .. %s: %d burst polls on %d APs, %d trigger alerts, %d corroborating alerts, "
          "%d episodes, %d in the window and above the floor, %d detections"
          % (fmt_ts(info["scan_from"]), fmt_ts(info["scan_to"]), info["bursts"], info["aps_with_bursts"],
             info["alerts"], info["corroborating"], info["episodes"], info["candidates"],
             info["findings"]), file=out)
    print(file=out)
    rows = []
    for i, ep in enumerate(e for e in episodes if e.finding):
        f = ep.finding
        rows.append((i + 1, (ep.ssid if ep.ssid else "<hidden>" if ep.ssid == "" else "<unknown>")[:22],
                     ep.bssid or ep.subject, fmt_ts(ep.first_ts), fmt_dur(ep.duration_s),
                     len(ep.alerts), fmt_dur(ep.alert_span_s) if ep.alerts else "-",
                     ",".join(ep.directions) or "-", len(ep.bursts),
                     "%d/%d" % (ep.max_in_window, ep.threshold),
                     "A" + ("+B" if ep.rule_b else "") if ep.rule_a else "B",
                     ep.severity,
                     "%s #%d" % (f.action, f.id) if f.action else
                     "would write" if args.dry_run else "-"))
    common.table(["#", "ap", "bssid", "start (UTC)", "dur", "alerts", "span", "dir",
                  "bursts", "win/thr", "rule", "sev", "action"],
                 rows, align_right={0, 4, 5, 6, 8, 9}, out=out)
    if args.verbose:
        print(file=out)
        print("episodes in the scan range (all, emitted or not):", file=out)
        for ep in episodes:
            _print_episode(ep, params, out)
    if args.json:
        print(json.dumps([{
            "id": f.id, "action": f.action, "ts": iso(f.ts), "type": f.type, "severity": f.severity,
            "device_key": f.device_key, "mac": f.mac, "ssid": f.ssid, "summary": f.summary,
            "evidence": f.evidence} for f in findings], indent=2, default=str))


def _print_episode(ep, params, out):
    status = ("-> %s (%s)" % (ep.severity, "A+B" if ep.rule_a and ep.rule_b else "A" if ep.rule_a else "B")
              if ep.emitted else "not flagged" if ep.threshold is not None else "outside the window / below the floor")
    print("  %s  %s .. %s  %s" % (ep.label(), fmt_ts(ep.first_ts), fmt_ts(ep.last_ts).split(" ")[1], status), file=out)
    events = [(a.ts, "alert", a) for a in ep.alerts + ep.corroborating] + [(b.ts, "burst", b) for b in ep.bursts]
    for ts, kind, obj in sorted(events, key=lambda e: e[0]):
        t = ts.astimezone(common.UTC).strftime("%H:%M:%S.%f")[:-3]
        if kind == "alert":
            print("      %s  alert  %-17s #%-5d %s -> %s (%s)" % (
                t, obj.header, obj.id, obj.source_mac or "?", obj.dest_mac or "?", obj.direction), file=out)
        else:
            print("      %s  burst  counter %d -> %d (previous poll %ds earlier)%s" % (
                t, obj.prev, obj.cur, obj.since_prev_s,
                ", non-data frames +%d" % obj.mgmt_delta if obj.mgmt_delta is not None else ""), file=out)
    b = ep.baseline
    if b:
        rate = _rate(b)
        print("      baseline%s: %.1f active h, %d burst polls (%.4f/h), %s flood alert(s); threshold %d in %s; "
              "non-data frames/poll median %s, robust sd %s" % (
                  " (scan range)" if b.get("fallback") else "", float(b["active_hours"] or 0), b["bursts"],
                  rate or 0.0, b.get("flood_alerts", "?"), ep.threshold, fmt_dur(params.burst_window_s),
                  _num(b.get("mgmt_med")), _num(b.get("mgmt_robust_sd"))), file=out)
    if ep.finding:
        print("      summary: %s" % ep.finding.summary, file=out)
