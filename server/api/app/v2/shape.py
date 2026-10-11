"""Row -> response helpers of the v2 endpoints that need no database.

Coverage (PLAN.md 7.2): capture seconds of a sensor in an hour =
(L - gap_s) x capture_polls / polls, where L is the part of the hour between
the sensor's first poll and its last poll + one poll interval (sensor_hourly
counts no gap before the first or after the last poll, so a whole hour is not
"captured" just because the last poll fell into it). A bucket's coverage is
its capture seconds / its seconds up to now.

Standard library only (tests/test_shape.py).
"""

from datetime import datetime, timedelta, timezone

from .periods import iso

POLL_S = 30            # nominal poll interval (sensor_config.poll_s of every version so far)
HOUR = timedelta(hours=1)
SEVERITIES = ("critical", "high", "medium", "low", "info")


def parse_ts(v):
    """A timestamp from json_agg ('2026-10-06T19:32:45+00:00') or psycopg (datetime)."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def hour_capture_s(row, first_poll, last_poll, poll_s=POLL_S):
    """Capture seconds of one sensor_hourly row (dict: hour, polls, capture_polls, gap_s)."""
    if not row["polls"] or first_poll is None:
        return 0.0
    hour = row["hour"]
    lo = max(hour, first_poll)
    hi = min(hour + HOUR, last_poll + timedelta(seconds=poll_s))
    span = (hi - lo).total_seconds()
    return max(0.0, span - row["gap_s"]) * row["capture_polls"] / row["polls"]


def coverage(period, hours, sensors):
    """{sensor name: [share of each bucket]} from sensor_hours rows.

    sensors: the selected sensors as dicts (id, name, first_poll, last_poll).
    """
    by_id = {s["id"]: s for s in sensors}
    secs = {s["id"]: [0.0] * len(period.starts) for s in sensors}
    i = 0
    for row in sorted(hours, key=lambda r: r["hour"]):
        s = by_id.get(row["sensor_id"])
        if s is None:
            continue
        while i < len(period.starts) - 1 and row["hour"] >= period.ends[i]:
            i += 1
        if not period.starts[i] <= row["hour"] < period.ends[i]:
            continue
        secs[s["id"]][i] += hour_capture_s(row, parse_ts(s["first_poll"]), parse_ts(s["last_poll"]))
    out = {}
    for s in sensors:
        shares = []
        for j, c in enumerate(secs[s["id"]]):
            total = period.seconds(j)
            shares.append(round(min(1.0, c / total), 3) if total > 0 else 0.0)
        out[s["name"]] = shares
    return out


def buckets(period, cov):
    """[{start, end, coverage: {sensor: share}}] and the per-bucket "any sensor collecting" flags."""
    names = list(cov)
    out, on = [], []
    for i, (a, b) in enumerate(zip(period.starts, period.ends)):
        c = {n: cov[n][i] for n in names}
        out.append({"start": iso(a), "end": iso(b), "coverage": c})
        on.append(any(v > 0 for v in c.values()))
    return out, on


def series(values, on):
    """Aligned with the buckets; None where no selected sensor was collecting (never a 0)."""
    return [v if flag else None for v, flag in zip(values, on)]


def by_bucket(rows, n, key, default=0):
    """rows with a 1-based bucket index `i` -> a list of row[key] of length n."""
    out = [default] * n
    for r in rows:
        out[r["i"] - 1] = r[key]
    return out


def window_share(hours, sensor, start, end):
    """Coverage of one sensor over [start, end): capture seconds / window seconds."""
    total = (end - start).total_seconds()
    if total <= 0:
        return None
    secs = sum(hour_capture_s(r, sensor["first_poll"], sensor["last_poll"])
               for r in hours if r["sensor_id"] == sensor["id"] and start <= r["hour"] < end)
    return round(min(1.0, secs / total), 3)


def captured_hours(hours):
    """UTC hours in which any selected sensor made a collecting poll."""
    return {r["hour"] for r in hours if r["capture_polls"]}


def bucket_of(period, t):
    """1-based bucket number of instant t, or None outside the period."""
    for i, (a, b) in enumerate(zip(period.starts, period.ends), start=1):
        if a <= t < b:
            return i
    return None


def clients_by_bucket(period, rows, captured):
    """Clients on managed APs per bucket from hourly sums (rows: hour, clients;
    one row per hour with load recorded, clients None = no managed AP in it).

    -> (peak per bucket or None, load share per bucket or None, (peak, hour)):
    load share = captured hours with load recorded / captured hours; a bucket
    whose captured hours have no load at all is None (load_missing).
    """
    n = len(period.starts)
    peak, loaded, cap = [None] * n, [0] * n, [0] * n
    best = (None, None)
    for h in captured:
        i = bucket_of(period, h)
        if i:
            cap[i - 1] += 1
    for r in rows:
        i = bucket_of(period, r["hour"])
        if i is None:
            continue
        v = r["clients"] or 0
        if r["hour"] in captured:
            loaded[i - 1] += 1
        peak[i - 1] = v if peak[i - 1] is None else max(peak[i - 1], v)
        if best[0] is None or v > best[0]:
            best = (v, r["hour"])
    share = [round(loaded[i] / cap[i], 3) if cap[i] else None for i in range(n)]
    return peak, share, best


SECURITY_LEVELS = (("open", "Open"), ("legacy", "Legacy (WEP, WPA1/TKIP)"),
                   ("psk_no_mfp", "WPA2-PSK without MFP"), ("strong", "Strong (MFP or Enterprise)"),
                   ("unknown", "Unknown"))


def security_rows(counts):
    """[{level, label, aps, share}] in the fixed order of the security scale."""
    by = {r["level"]: r["n"] for r in counts}
    total = sum(by.values())
    return [{"level": lv, "label": label, "aps": by.get(lv, 0),
             "share": round(by.get(lv, 0) / total, 4) if total else 0.0}
            for lv, label in SECURITY_LEVELS]


def severity_rank(sev):
    return SEVERITIES.index(sev) if sev in SEVERITIES else len(SEVERITIES)


# compliance reasons below the policy level (anything else - MFP, unknown,
# not seen - is a low finding)
WEAKER = {"not Enterprise", "weaker than WPA2-PSK", "not WPA3"}


def compliance(networks):
    """(summary, findings) of the managed networks (rows: network, bssids, failing, reason)."""
    problems = []
    for n in networks:
        if n["bssids"] == 0:
            problems.append(dict(n, reason="not seen by the selected sensors"))
        elif n["failing"]:
            problems.append(n)
    met = len(networks) - len(problems)
    first = None
    if problems:
        p = problems[0]
        first = {"network": p["network"], "reason": p["reason"], "failing": p["failing"], "bssids": p["bssids"]}
    findings = [{
        "severity": "medium" if p["reason"] in WEAKER else "low",
        "kind": "compliance",
        "title": "Managed network %s: %s" % (p["network"], p["reason"]),
        "network": p["network"],
        "detail": ("%d of %d BSSIDs" % (p["failing"], p["bssids"])) if p["bssids"] else None,
        "link": "/networks",
    } for p in problems]
    return {"met": met, "total": len(networks), "first_problem": first}, findings


def attention(events, findings, limit=10):
    """Open events (severity >= medium) and compliance findings, the most
    severe first, events newest first, findings after the events of their
    severity."""
    items = [{
        "severity": e["severity"], "kind": e["kind"], "type": e["type"], "title": e["summary"],
        "bssid": e["bssid"], "network": e["ssid"], "sensors": [e["sensor"]], "ts": e["ts"],
        "ts_end": e["ts_end"], "event_id": e["id"],
        "link": "/sensors" if e["kind"] == "sensor" else "/events?id=%d" % e["id"],
    } for e in events]
    events_total = events[0]["total"] if events else 0
    ranked = sorted(enumerate(items + findings),
                    key=lambda t: (severity_rank(t[1]["severity"]), t[1].get("event_id") is None, t[0]))
    out = [x for _, x in ranked]
    return out[:limit], events_total + len(findings)


def sensor_status(last_capture_at, last_poll, now, offline_min):
    """online | offline | no_data and the seconds since the last collecting poll.

    Until live ingest the data ends at the last import, so every sensor reads
    offline between imports (SPEC Q7: the Sensors page shows "data until").
    """
    if last_poll is None:
        return "no_data", None
    if last_capture_at is None:
        return "offline", None
    offline_s = max(0, int((now - last_capture_at).total_seconds()))
    return ("online" if offline_s <= offline_min * 60 else "offline"), offline_s


def severity_counts(rows):
    """{severity: n} with every level present (rows: severity, n)."""
    out = {s: 0 for s in SEVERITIES}
    for r in rows:
        out[r["severity"]] = r["n"]
    return out


def utc_now():
    return datetime.now(timezone.utc)
