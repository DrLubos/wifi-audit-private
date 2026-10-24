"""Dataset overview: the CSVs of export.sh -> dataset.md (the dataset
description for the thesis). Pure formatting, standard library only."""

import csv
import os
from datetime import datetime, timezone

SECURITY_ORDER = ("open", "legacy", "psk_no_mfp", "strong", "unknown")
BAND_ORDER = ("2.4", "5", "6", "unknown")


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _cell(v):
    return "" if v is None else str(v).replace("|", "\\|").replace("\n", " ")


def md_table(head, rows):
    out = ["| " + " | ".join(_cell(h) for h in head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def fmt_duration(s):
    if s in ("", None):
        return ""
    s = int(float(s))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if s < 60:
        return "%d s" % sec
    if s < 3600:
        return "%d m %d s" % (m, sec)
    if s < 86400:
        return "%d h %d m" % (h, m)
    return "%d d %d h %d m" % (d, h, m)


def _ordered(values, order):
    known = [v for v in order if v in values]
    return known + sorted(v for v in values if v not in order)


def pivot(rows, row_key, col_key, value, col_order=(), row_order=()):
    """GROUPING SETS rows (totals as '*') -> (head, body): one row per
    row_key, one column per col_key, totals as the last row / column 'all'."""
    cols = _ordered({r[col_key] for r in rows if r[col_key] != "*"}, col_order)
    rws = _ordered({r[row_key] for r in rows if r[row_key] != "*"}, row_order)
    cell = {(r[row_key], r[col_key]): r[value] for r in rows}
    head = [row_key] + cols + ["all"]
    body = []
    for rk in rws + ["*"]:
        if not any(k[0] == rk for k in cell):
            continue
        body.append(["all" if rk == "*" else rk] + [cell.get((rk, c), "0") for c in cols] + [cell.get((rk, "*"), "0")])
    return head, body


def render(d, sensor, frm, to):
    period = read(os.path.join(d, "period.csv"))
    outages = read(os.path.join(d, "outages.csv"))
    bounds = read(os.path.join(d, "boundaries.csv"))
    aps = read(os.path.join(d, "aps.csv"))
    events = read(os.path.join(d, "events.csv"))
    alerts = read(os.path.join(d, "alerts.csv"))
    md = ["# Dataset overview - sensor %s" % sensor, "",
          "Range %s - %s (UTC), exported %s by `server/evaluation/dataset/export.sh` (read-only). "
          "Definitions: `server/evaluation/dataset/README.md`." % (
              frm, to, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), ""]

    md += ["## Collection period, polls and coverage", "",
           md_table(["metric", "value", "unit", "note"], [[r["metric"], r["value"], r["unit"], r["note"]] for r in period]),
           ""]

    o = [r for r in outages if r["kind"] == "outage"]
    li = [r for r in outages if r["kind"] == "limited"]
    md += ["## Outages and limited capture", "",
           "%d outages (%s in total), %d limited-capture periods (%s)." % (
               len(o), fmt_duration(sum(float(r["duration_s"] or 0) for r in o)),
               len(li), fmt_duration(sum(float(r["duration_s"] or 0) for r in li))), "",
           md_table(["kind", "from", "to", "length", "polls", "recorded cause"],
                    [[r["kind"], r["starts_at"], r["ends_at"] or "ongoing", fmt_duration(r["duration_s"]), r["polls"],
                      r["cause"]] for r in outages]), ""]

    md += ["## Configuration history (dataset boundaries)", "",
           md_table(["from", "to", "kind", "boundary", "label", "ref"],
                    [[r["starts_at"], r["ends_at"], r["kind"], "yes" if r["boundary"] == "t" else "",
                      r["label"], r["ref"]] for r in bounds]), ""]

    md += ["## APs and networks per band and security level", "",
           "Security level and band of each AP's current configuration (`account_aps`); a network is an "
           "SSID (or the name a hidden AP revealed).", "", "**APs**", ""]
    h, b = pivot(aps, "band", "security", "aps", SECURITY_ORDER, BAND_ORDER)
    md += [md_table(h, b), "", "**Networks**", ""]
    h, b = pivot(aps, "band", "security", "networks", SECURITY_ORDER, BAND_ORDER)
    md += [md_table(h, b), ""]
    extra = ["hidden_aps", "hidden_unnamed_aps", "random_bssid_aps", "mobile_aps", "managed_aps", "friendly_aps",
             "suspicious_aps", "ignored_aps"]
    per_band = [r for r in aps if r["security"] == "*"]
    per_band.sort(key=lambda r: (r["band"] == "*", BAND_ORDER.index(r["band"]) if r["band"] in BAND_ORDER else 9))
    md += ["**Per band**", "",
           md_table(["band", "aps", "networks"] + extra,
                    [["all" if r["band"] == "*" else r["band"], r["aps"], r["networks"]] + [r.get(k, "") for k in extra]
                     for r in per_band]), ""]

    md += ["## Events per kind, type and severity", "",
           md_table(["kind", "type", "severity", "events", "open", "confirmed", "benign", "false positive"],
                    [[r["kind"], "all" if r["type"] == "*" else r["type"], "" if r["severity"] == "*" else r["severity"],
                      r["events"], r["open"], r["confirmed"], r["benign"], r["false_positive"]] for r in events]), ""]

    md += ["## Kismet alerts per header", "",
           md_table(["header", "alerts", "becomes an event", "first", "last"],
                    [[r["header"], r["alerts"], "yes" if r["as_event"] == "t" else "no", r["first"], r["last"]]
                     for r in alerts]), ""]
    return "\n".join(md)
