"""GET /api/v2/spectrum and /api/v2/spectrum/utilization (Spectrum board).

Fixed APs only (spectrum counts exclude hotspots), each counted once on its
primary channel in the period (sql/spectrum.sql). Definitions (PLAN.md 7.3):
overlapping 2.4 GHz = primary channel not 1, 6 or 11; 40 MHz in 2.4 GHz =
HT40+/-; busiest = the channel with the most APs, its daytime utilization =
p25-p75 of its hourly values 08:00-18:00 local; channel-change frequency =
changes per AP per observed day, p25-p75 per network, networks with a
median >= 4 a day; channel plan = APs on 1/6/11 vs on 5/9/13. DFS =
5 GHz channels 52-144.
"""

from statistics import median
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request

from .. import deps, periods, queries, shape
from ..shape import band_of, dfs, iqr
from ..periods import iso

router = APIRouter(prefix="/spectrum", tags=["v2 spectrum"])

_Q = queries.load("spectrum")
_QC = queries.load("context")

CHANNELS = {"2.4": [str(c) for c in range(1, 14)],
            "5": [str(c) for c in (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128,
                                   132, 136, 140, 144, 149, 153, 157, 161, 165)]}
NON_OVERLAPPING = {"1", "6", "11"}
BUSY_CHANGES = 4.0       # channel changes per AP and observed day that make a network "dynamic"


def _args(acc, sel, p, band):
    return {"account_id": acc["id"], "sensor_ids": sel["ids"], "account_latest": sel["account_latest"],
            "from": p.start, "to": p.ends[-1], "tz": acc["tz"], "band": band}


@router.get("")
def spectrum(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
             range_: str = Depends(deps.range_query), band: str = Query("2.4", pattern=r"^(2\.4|5)$")):
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch)
    if cached:
        return cached
    a = _args(acc, sel, p, band)
    with deps.connection() as conn:
        r = deps.run(conn, _Q, "spectrum", **{k: a[k] for k in ("account_id", "sensor_ids", "account_latest",
                                                               "from", "to", "tz")})[0]
    aps, util = r["aps"], r["util"]
    local = ZoneInfo(acc["tz"])
    in_band = [x for x in aps if band_of(x["channel"]) == band]
    per_channel = {}
    for x in in_band:
        per_channel[x["channel"]] = per_channel.get(x["channel"], 0) + 1
    hourly = {}                               # channel -> [hourly utilization]
    daytime = {}                              # channel -> [hourly utilization 08:00-18:00 local]
    for u in util:
        if band_of(u["channel"]) != band or u["util"] is None:
            continue
        v = float(u["util"])
        hourly.setdefault(u["channel"], []).append(v)
        if 8 <= shape.parse_ts(u["hour"]).astimezone(local).hour < 18:
            daytime.setdefault(u["channel"], []).append(v)
    names = sorted(set(CHANNELS[band]) | set(per_channel) | set(hourly), key=int)
    channels = [{"channel": c, "aps": per_channel.get(c, 0),
                 "util_avg": round(sum(hourly[c]) / len(hourly[c]), 1) if c in hourly else None,
                 "dfs": dfs(c), "overlaps": band == "2.4" and c not in NON_OVERLAPPING} for c in names]
    busiest = max(channels, key=lambda c: (c["aps"], -int(c["channel"])), default=None)
    g24 = [x for x in aps if band_of(x["channel"]) == "2.4"]
    rates = {}
    for x in aps:
        if x["days"]:
            rates.setdefault(x["network"], []).append(x["changes"] / x["days"])
    dynamic = sorted(({"network": n, "aps": len(v), "median": round(median(v), 1), "per_day": iqr(v)}
                      for n, v in rates.items() if median(v) >= BUSY_CHANGES),
                     key=lambda d: (-d["median"], d["network"]))
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc), band=band)
    body.update({
        "aps": len(in_band),
        "current_channel_aps": sum(1 for x in in_band if x["current"]),
        "channels": channels,
        "issues": {
            "overlapping": sum(1 for x in g24 if x["channel"] not in NON_OVERLAPPING),
            "wide_2g": sum(1 for x in g24 if (x["ht_mode"] or "").startswith("HT40")),
            "busiest": None if not busiest or not busiest["aps"] else {
                "channel": busiest["channel"], "aps": busiest["aps"],
                "day_util": iqr(daytime.get(busiest["channel"], []))},
            "channel_changes": dynamic,
            "plan": {"ch_1_6_11": sum(1 for x in g24 if x["channel"] in ("1", "6", "11")),
                     "ch_5_9_13": sum(1 for x in g24 if x["channel"] in ("5", "9", "13"))},
        },
    })
    return deps.respond(body, tag)


@router.get("/utilization")
def utilization(request: Request, acc=Depends(deps.get_account), sensor: str = Depends(deps.sensor_query),
                range_: str = Depends(deps.range_query), band: str = Query("2.4", pattern=r"^(2\.4|5)$"),
                view: str = Query("day", pattern="^(day|timeline)$")):
    """Rows per channel of the band with at least one reporting AP. day: 24
    local hours, each the mean over the period (peak = the highest hour);
    timeline: 24h -> 24 x 1 h, 7d -> 168 x 1 h, 30d -> 180 x 4 h (local
    blocks). Cells {start, avg, peak, aps}; null = no report."""
    sel = deps.select_sensors(acc, sensor)
    p = deps.period_of(acc, range_)
    tag, cached = deps.etag(request, acc, p.epoch)
    if cached:
        return cached
    a = _args(acc, sel, p, band)
    with deps.connection() as conn:
        rows = deps.run(conn, _Q, "utilization", **{k: a[k] for k in ("account_id", "sensor_ids",
                                                                      "account_latest", "from", "to", "band")})
        hours = deps.run(conn, _QC, "sensor_hours", sensor_ids=sel["ids"], **{"from": p.start, "to": p.ends[-1]})
    local = ZoneInfo(acc["tz"])
    captured = shape.captured_hours(hours)
    by_ch = {}
    for r in rows:
        by_ch.setdefault(r["channel"], []).append(r)
    if view == "day":
        cols = [(h, None, None) for h in range(24)]
    elif p.range == "30d":
        cols = [(None, s, e) for s, e in periods.blocks(p, 4)]
    else:
        cols = []
        t = p.start
        while t < p.ends[-1]:
            cols.append((None, t, t + shape.HOUR))
            t += shape.HOUR
    out = []
    for ch in sorted(by_ch, key=int):
        cells = [{"vals": [], "aps": 0} for _ in cols]
        for r in by_ch[ch]:
            if view == "day":
                i = r["hour"].astimezone(local).hour
            else:
                i = next((k for k, (_, s, e) in enumerate(cols) if s <= r["hour"] < e), None)
                if i is None:
                    continue
            cells[i]["vals"].append(float(r["util"]))
            cells[i]["aps"] = max(cells[i]["aps"], r["aps"])
        row = []
        for (hr, s, e), c in zip(cols, cells):
            v = c["vals"]
            cell = {"avg": round(sum(v) / len(v), 1) if v else None, "peak": round(max(v), 1) if v else None,
                    "aps": c["aps"] or None}
            if view == "day":
                cell["hour"] = hr
            else:
                cell.update({"start": iso(s), "end": iso(e),
                             "captured": any(s <= h < e for h in captured)})
            row.append(cell)
        out.append({"channel": ch, "dfs": dfs(ch), "cells": row})
    body = dict(p.envelope(sel["filter"]), account=deps.account_head(acc), band=band, view=view)
    body.update({"cells": len(cols), "rows": out})
    return deps.respond(body, tag)
