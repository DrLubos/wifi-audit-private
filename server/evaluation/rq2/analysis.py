"""RQ2 (false positives): per kind / type / rule counts and rates of the
operator's labels, the reasons grouped, and the share of events traced to a
known cause. Pure functions over the rows events.sql exports; standard
library only. README.md defines every metric.

A cause comes from two places:
  note  - the operator's tag `cause:<slug>` in the label note (authoritative);
  rule  - a documented, deterministic reading of the data (RULES below).
The note wins; the rule's answer is kept in `rule_cause` so the two can be
compared (agreement.csv).
"""

import csv
import os
import re
from collections import Counter, OrderedDict

from ..metrics import wilson_ci

# events.sql's columns, in order
EVENT_COLUMNS = (
    "id", "sensor", "kind", "type", "rule", "severity", "ts", "ts_end", "duration_s", "n_alerts",
    "has_subject", "network_kind", "status", "status_reason", "status_at", "note",
    "ap_level_at", "ap_mfp_at", "in_outage", "in_limited", "degraded_kinds", "channel_change_1h",
    "reverted_within_s", "backlog_on_server", "classification_now", "oui_allowed_later", "baseline_median",
)

# the vocabulary of causes (README.md); `cause:<slug>` in a note
CAUSES = OrderedDict([
    ("rssi_floor", "RSSI floor censoring: the RTL8821CU's -106/-120 dBm sentinel read as a level "
                   "(findings.md 10-11)"),
    ("channel_hopping", "channel hopping / reduced channel coverage: hop list collapsed or halved, "
                        "off-channel reception (findings.md 7)"),
    ("kismet_rule", "a Kismet rule that does not describe a security event or is defective "
                    "(NOCLIENTMFP, findings.md 12)"),
    ("allow_list_incomplete", "the network's allow-list (BSSIDs / OUIs) was incomplete at the time"),
    ("pipeline_bug", "a bug in this project's own pipeline (e.g. event 866: a retention-risk event "
                     "for a backlog the server already held)"),
    ("sensor_degraded", "the capture was down or delivered no frames at the time (not hopping)"),
    ("ap_behaviour", "normal behaviour of the AP (dynamic channel selection, DFS, reboot, its owner's change)"),
    ("client_behaviour", "normal behaviour of clients (roaming, leaving, power save)"),
    ("staged_test", "this project's own staged test"),
    ("other", "another cause (named in the note), or an unknown slug"),
])

SECURITY_KINDS = ("detection", "kismet", "config")
STATUSES = ("open", "confirmed", "benign", "false_positive")
FLOOR_BASELINE_DBM = -100.0      # an RSSI baseline at or below this sits on the censoring floor

MAC_RE = re.compile(r"(?i)\b([0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
_TAG_RE = re.compile(r"(?i)\bcause\s*:\s*([a-z0-9_]+)")

OUTPUTS = ("events.csv", "by_rule.csv", "by_reason.csv", "by_cause.csv", "agreement.csv", "summary.csv")


def _t(v):
    return str(v).strip().lower() in ("t", "true", "1", "yes")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def note_cause(note):
    """The first `cause:<slug>` in a note -> (slug, unknown slug or None).
    An unknown slug counts as 'other' and is reported."""
    m = _TAG_RE.search(note or "")
    if not m:
        return None, None
    slug = m.group(1).lower()
    if slug in CAUSES:
        return slug, None
    return "other", slug


def rule_cause(row):
    """(cause, detail) from the data, or (None, None). First match wins; each
    rule is documented in README.md ("Rule-based causes")."""
    kind, typ = row["kind"], row["type"]
    if typ == "upload_retention_risk" and _t(row["backlog_on_server"]):
        return "pipeline_bug", "retention-risk event for polls the server already held (fixed in 847404f)"
    if typ == "NOCLIENTMFP":
        if row["ap_level_at"] == "open":
            return "kismet_rule", "association request to an open network: MFP impossible, no RSN element"
        if row["ap_mfp_at"] == "no" and row["ap_level_at"] not in ("", "unknown"):
            return "kismet_rule", "MFP not offered by the AP: the AP's security level already records it"
        return None, None
    if typ == "unknown_bssid_managed_ssid" and (
            _t(row["oui_allowed_later"]) or row["classification_now"] in ("managed", "friendly")):
        return "allow_list_incomplete", "the BSSID is allowed now (%s)" % (
            "its OUI was added to the network later" if _t(row["oui_allowed_later"])
            else "classified %s now" % row["classification_now"])
    if typ == "evil_twin":
        b = _f(row["baseline_median"])
        if b is not None and b <= FLOOR_BASELINE_DBM:
            return "rssi_floor", "baseline median %.0f dBm at the censoring floor" % b
    if kind in SECURITY_KINDS:
        degraded = set(filter(None, (row["degraded_kinds"] or "").split("|")))
        if _t(row["in_limited"]) or "coverage" in degraded:
            return "channel_hopping", "inside a period of reduced channel coverage"
        if _t(row["in_outage"]) or degraded & {"gap", "no_frames"}:
            return "sensor_degraded", "inside a capture outage / no-frames window"
    return None, None


def assign(row):
    """The row with scope, the operator's and the rule's cause, and the one used."""
    r = dict(row)
    r["scope"] = "security" if row["kind"] in SECURITY_KINDS else "sensor"
    rc, detail = rule_cause(row)
    nc, unknown = note_cause(row.get("note"))
    r["rule_cause"], r["rule_detail"] = rc or "", detail or ""
    r["note_cause"], r["note_unknown_slug"] = nc or "", unknown or ""
    if nc:
        r["cause"], r["cause_source"] = nc, "note"
    elif rc:
        r["cause"], r["cause_source"] = rc, "rule"
    else:
        r["cause"], r["cause_source"] = "", "none"
    return r


def _rate(k, n):
    return k / n if n else ""


def _metrics(rows):
    c = Counter(r["status"] for r in rows)
    n = len(rows)
    lab = n - c["open"]
    m = OrderedDict(n=n, open=c["open"], labelled=lab, confirmed=c["confirmed"], benign=c["benign"],
                    false_positive=c["false_positive"], label_coverage=_rate(lab, n))
    for name, k in (("confirmed", c["confirmed"]), ("benign", c["benign"]), ("fp", c["false_positive"]),
                    ("not_security", c["benign"] + c["false_positive"])):
        m[name + "_rate"] = _rate(k, lab)
        lo, hi = wilson_ci(k, lab) if lab else ("", "")
        m[name + "_rate_lo"], m[name + "_rate_hi"] = lo, hi
    traced = [r for r in rows if r["cause"]]
    m["traced"] = len(traced)
    m["traced_rule"] = sum(1 for r in traced if r["cause_source"] == "rule")
    m["traced_note"] = sum(1 for r in traced if r["cause_source"] == "note")
    m["traced_share"] = _rate(len(traced), n)
    ns = [r for r in rows if r["status"] in ("benign", "false_positive")]
    m["not_security_traced_share"] = _rate(sum(1 for r in ns if r["cause"]), len(ns))
    return m


def _groups(rows, key):
    g = OrderedDict()
    for r in sorted(rows, key=key):
        g.setdefault(key(r), []).append(r)
    return g


def by_rule(rows):
    """One row per (kind, type, rule), then per (kind, type), then totals per
    scope ('*security', '*sensor') and '*all'."""
    out = []
    for (k, t, ru), rs in _groups(rows, lambda r: (r["kind"], r["type"], r["rule"])).items():
        out.append(OrderedDict(kind=k, type=t, rule=ru, **_metrics(rs)))
    for (k, t), rs in _groups(rows, lambda r: (r["kind"], r["type"])).items():
        if len({r["rule"] for r in rs}) > 1:
            out.append(OrderedDict(kind=k, type=t, rule="*", **_metrics(rs)))
    for sc, rs in _groups(rows, lambda r: r["scope"]).items():
        out.append(OrderedDict(kind="*" + sc, type="*", rule="*", **_metrics(rs)))
    if rows:
        out.append(OrderedDict(kind="*all", type="*", rule="*", **_metrics(rows)))
    return out


def by_reason(rows):
    """Counts per (kind, type, status, reason) and over all events, with the
    share of the type's events and of the type's events in that status."""
    out = []
    def emit(kind, typ, rs):
        n = len(rs)
        per_status = Counter(r["status"] for r in rs)
        for (st, rea), k in sorted(Counter((r["status"], r["status_reason"]) for r in rs).items(),
                                   key=lambda x: (STATUSES.index(x[0][0]) if x[0][0] in STATUSES else 9, x[0][1])):
            out.append(OrderedDict(kind=kind, type=typ, status=st, status_reason=rea, n=k,
                                   share_of_type=_rate(k, n), share_of_status=_rate(k, per_status[st])))
    for (k, t), rs in _groups(rows, lambda r: (r["kind"], r["type"])).items():
        emit(k, t, rs)
    if rows:
        emit("*all", "*", rows)
    return out


def by_cause(rows):
    """Counts per (scope, kind, type, cause, source); '*' rows over all types
    of a scope and over all events."""
    out = []
    def emit(scope, kind, typ, rs):
        n = len(rs)
        for (cause, src), k in sorted(Counter((r["cause"], r["cause_source"]) for r in rs).items()):
            st = Counter(r["status"] for r in rs if (r["cause"], r["cause_source"]) == (cause, src))
            out.append(OrderedDict(scope=scope, kind=kind, type=typ, cause=cause, source=src, n=k,
                                   share_of_type=_rate(k, n), open=st["open"], confirmed=st["confirmed"],
                                   benign=st["benign"], false_positive=st["false_positive"],
                                   description=CAUSES.get(cause, "")))
    for (sc, k, t), rs in _groups(rows, lambda r: (r["scope"], r["kind"], r["type"])).items():
        emit(sc, k, t, rs)
    for sc, rs in _groups(rows, lambda r: r["scope"]).items():
        emit(sc, "*", "*", rs)
    if rows:
        emit("*all", "*", "*", rows)
    return out


def agreement(rows):
    """Rule cause vs the operator's tag, for events that have a tag."""
    c = Counter((r["rule_cause"], r["note_cause"]) for r in rows if r["note_cause"])
    return [OrderedDict(rule_cause=rc, note_cause=nc, n=k, agree=rc == nc) for (rc, nc), k in sorted(c.items())]


def summary(rows):
    sec = [r for r in rows if r["scope"] == "security"]
    lab = [r for r in rows if r["status"] != "open"]
    ns = [r for r in rows if r["status"] in ("benign", "false_positive")]
    fp = [r for r in rows if r["status"] == "false_positive"]
    unknown = sorted({r["note_unknown_slug"] for r in rows if r["note_unknown_slug"]})
    items = [
        ("events", len(rows), "all kinds"),
        ("security_events", len(sec), "kinds detection, kismet, config"),
        ("sensor_events", len(rows) - len(sec), "kind sensor (outages, upload stalls)"),
        ("labelled", len(lab), "status other than open"),
        ("label_coverage", _rate(len(lab), len(rows)), "labelled / events"),
        ("security_labelled", sum(1 for r in sec if r["status"] != "open"), ""),
        ("confirmed", sum(1 for r in rows if r["status"] == "confirmed"), ""),
        ("benign", sum(1 for r in rows if r["status"] == "benign"), ""),
        ("false_positive", len(fp), ""),
        ("fp_rate", _rate(len(fp), len(lab)), "false_positive / labelled"),
        ("events_traced", sum(1 for r in rows if r["cause"]), "a cause from a note tag or a rule"),
        ("traced_share", _rate(sum(1 for r in rows if r["cause"]), len(rows)), "events_traced / events"),
        ("traced_by_note", sum(1 for r in rows if r["cause_source"] == "note"), ""),
        ("traced_by_rule", sum(1 for r in rows if r["cause_source"] == "rule"), ""),
        ("security_traced_share", _rate(sum(1 for r in sec if r["cause"]), len(sec)), ""),
        ("not_security_labelled", len(ns), "benign + false_positive"),
        ("not_security_traced_share", _rate(sum(1 for r in ns if r["cause"]), len(ns)),
         "of benign + false_positive, the share with a known cause"),
        ("fp_traced_share", _rate(sum(1 for r in fp if r["cause"]), len(fp)), ""),
        ("note_unknown_slugs", "|".join(unknown), "cause tags not in the vocabulary (counted as other)"),
    ]
    for cause in CAUSES:
        items.append(("cause_" + cause, sum(1 for r in rows if r["cause"] == cause), CAUSES[cause]))
    return [OrderedDict(metric=m, value=v, note=n) for m, v, n in items]


# --- files ---------------------------------------------------------------------------------------

def read_events(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    missing = [c for c in EVENT_COLUMNS if rows and c not in rows[0]]
    if missing:
        raise ValueError("%s: missing columns %s" % (path, ", ".join(missing)))
    for r in rows:
        r["note"] = MAC_RE.sub("<mac>", r.get("note") or "")    # events.sql masks too; never trust one side
    return rows


def _fmt(v):
    if isinstance(v, float):
        return "%.6f" % v
    if isinstance(v, bool):
        return "t" if v else "f"
    return v


def write_csv(path, rows, fields=None):
    fields = fields or (list(rows[0].keys()) if rows else ["empty"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k, "")) for k in fields})


EVENT_OUT = list(EVENT_COLUMNS) + ["scope", "cause", "cause_source", "rule_cause", "rule_detail",
                                   "note_cause", "note_unknown_slug"]


def run(events_csv, out_dir):
    rows = [assign(r) for r in read_events(events_csv)]
    os.makedirs(out_dir, exist_ok=True)
    write_csv(os.path.join(out_dir, "events.csv"), rows, EVENT_OUT)
    write_csv(os.path.join(out_dir, "by_rule.csv"), by_rule(rows))
    write_csv(os.path.join(out_dir, "by_reason.csv"), by_reason(rows))
    write_csv(os.path.join(out_dir, "by_cause.csv"), by_cause(rows))
    write_csv(os.path.join(out_dir, "agreement.csv"), agreement(rows),
              ["rule_cause", "note_cause", "n", "agree"])
    write_csv(os.path.join(out_dir, "summary.csv"), summary(rows))
    return rows
