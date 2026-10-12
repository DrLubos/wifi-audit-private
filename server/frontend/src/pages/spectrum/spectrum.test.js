import { test, eq, done } from "../../testing/tiny.js";
import {
  dynamicText, hoursText, issueItems, occNote, occupancy, rangeOf, utilCellText, utilColor, utilText, utilTicks,
} from "./spectrum.js";

const TZ = "Europe/Bratislava";

test("occupancy columns", () => {
  const ch = (channel, aps, util_avg = null, dfs = false, overlaps = false) => ({ channel, aps, util_avg, dfs, overlaps });
  const g = occupancy([ch("1", 10, 12.9), ch("2", 4, null, false, true), ch("4", 0, 15.4, false, true),
                       ch("6", 11, 9.0)], "2.4");
  eq(g.map((c) => [c.ch, c.n, c.good, c.h]), [["1", 10, true, "75%"], ["2", 4, false, "30%"], ["4", 0, false, "0%"],
                                               ["6", 11, true, "82%"]]);
  eq(g[0].rows, [{ c: "var(--c1)", v: "10", l: "APs in the period" },
                 { c: "transparent", v: "13 %", l: "average utilization" }]);
  eq(g[1].notes, ["Overlaps with neighbouring channels"]);
  eq(g[0].aria, "Channel 1: 10 APs, average utilization 13 %");
  // utilization but no AP on it as its main channel: the APs passing through
  const p = occupancy([{ ...ch("4", 0, 15.4, false, true), passing: 3 }, { ...ch("1", 10, 12.9), passing: 2 }], "2.4");
  eq(p[0].rows, [{ c: "var(--c2)", v: "0", l: "APs in the period" },
                 { c: "transparent", v: "3", l: "APs passing through" },
                 { c: "transparent", v: "15 %", l: "average utilization" }]);
  eq(p[0].aria, "Channel 4: 0 APs, 3 passing through, average utilization 15 %");
  eq(p[1].rows.length, 2);                                   // APs on it: no passing-through row
  // 5 GHz: only the channels with APs, DFS orange
  const f = occupancy([ch("36", 4, 16.1), ch("40", 0, 13.8), ch("52", 3, 13.2, true)], "5");
  eq(f.map((c) => [c.ch, c.good, c.notes[0]]), [["36", true, "Non-DFS channel"],
                                                ["52", false, "DFS: the AP must leave the channel when it detects radar"]]);
  eq(occNote("5", 0), "5 GHz channels do not overlap. On DFS channels the AP must change channel when it detects "
     + "radar. Each AP counts once, on the channel it used most in the period.");
});

test("channel plan issues", () => {
  eq(rangeOf([8.3, 9.8]), "8–10");
  eq(rangeOf([11.8, 11.8]), "12");
  eq(rangeOf(null), null);
  const dyn = [
    { network: "hidden:EC:58:EA:95:8E:EC", aps: 1, median: 11.8, per_day: [11.8, 11.8] },
    { network: "UZVD-zamestnanci", aps: 13, median: 10.0, per_day: [7.2, 11.2] },
    { network: "IK-WIFI-DOT1X", aps: 28, median: 8.6, per_day: [2.8, 9.2] },
    { network: "hidden:EC:58:EA:94:22:DC", aps: 1, median: 9.0, per_day: [9.0, 9.0] },
    { network: "IK-WIFI", aps: 28, median: 8.4, per_day: [2.8, 9.8] },
  ];
  eq(dynamicText(dyn), "UZVD-zamestnanci 7–11×, IK-WIFI-DOT1X 3–9×, IK-WIFI 3–10× per day and 2 hidden networks");
  eq(dynamicText(dyn, 1), "UZVD-zamestnanci 7–11× per day, 2 more networks and 2 hidden networks");
  eq(dynamicText(dyn.slice(0, 1)), "1 hidden network");
  eq(dynamicText(dyn.slice(1, 3)), "UZVD-zamestnanci 7–11×, IK-WIFI-DOT1X 3–9× per day");
  const items = issueItems({ overlapping: 42, wide_2g: 0,
                             busiest: { channel: "6", aps: 11, busy_hours: { from: 21, to: 25, util: [12.2, 14.8] } },
                             channel_changes: dyn, plan: { ch_1_6_11: 23, ch_5_9_13: 12 } }, "2.4");
  eq(items.map((i) => [i.cls, i.badge]), [["warn", "42 APs"], ["ok", "0 APs"], ["neu", "Channel 6"],
                                          ["info", "5 networks"], ["neu", "1/5/9/13 plan"]]);
  eq(items[2].text, "busiest in 2.4 GHz: 11 APs; its busiest hours 21:00–01:00, utilization 12–15 %");
  eq(hoursText({ from: 20, to: 24 }), "20:00–24:00");
  eq(hoursText({ from: 9, to: 13 }), "09:00–13:00");
  eq(issueItems({ overlapping: 0, wide_2g: 0, busiest: { channel: "48", aps: 7, busy_hours: null }, channel_changes: [],
                  plan: { ch_1_6_11: 0, ch_5_9_13: 0 } }, "5")[2].text, "busiest in 5 GHz: 7 APs; no utilization reported");
  eq(items[4].text, "in use nearby too: channels 5, 9 and 13 have 12 APs together, channels 1, 6 and 11 have 23");
  const quiet = issueItems({ overlapping: 0, wide_2g: 0, busiest: null, channel_changes: [],
                             plan: { ch_1_6_11: 40, ch_5_9_13: 3 } }, "5");
  eq(quiet.map((i) => i.badge), ["0 APs", "0 APs", "Stable", "1/5/9/13 plan"]);
  eq(quiet[3].text, "not used nearby: channels 5, 9 and 13 have 3 APs together, channels 1, 6 and 11 have 40");
});

test("utilization cells", () => {
  eq(utilText(12.94), "13 %");
  const now = "2026-10-12T10:40:00Z";
  eq(utilCellText("day", "6", { hour: 9, avg: 12.4, peak: 30.2, aps: 4 }, TZ, now),
     "Channel 6 · 09:00–10:00 · average utilization 12 %, highest hour 30 % · 4 APs reporting");
  eq(utilCellText("day", "6", { hour: 23, avg: null, peak: null, aps: null }, TZ, now),
     "Channel 6 · 23:00–00:00 · no utilization reported");
  const hour = (start, end, avg, extra = {}) => ({ start, end, avg, peak: avg, aps: avg == null ? null : 1, captured: true, ...extra });
  eq(utilCellText("timeline", "1", hour("2026-10-11T08:00:00Z", "2026-10-11T09:00:00Z", 9.6), TZ, now),
     "Channel 1 · Sun 11 Oct, 10:00–11:00 · utilization 10 % · 1 AP reporting");
  eq(utilCellText("timeline", "1", hour("2026-10-08T08:00:00Z", "2026-10-08T09:00:00Z", null, { captured: false }), TZ, now),
     "Channel 1 · Thu 8 Oct, 10:00–11:00 · no sensor collecting");
  eq(utilCellText("timeline", "1", hour("2026-10-05T16:00:00Z", "2026-10-05T17:00:00Z", null), TZ, now),
     "Channel 1 · Mon 5 Oct, 18:00–19:00 · no AP reported utilization");
  eq(utilCellText("timeline", "1", hour("2026-10-12T11:00:00Z", "2026-10-12T12:00:00Z", null), TZ, now),
     "Channel 1 · Mon 12 Oct, 13:00–14:00 · not yet");
  // a 4-hour block ending at midnight, a spike
  eq(utilCellText("timeline", "6", { start: "2026-10-10T18:00:00Z", end: "2026-10-10T22:00:00Z", avg: 40.2, peak: 96,
                                    aps: 3, captured: true }, TZ, now),
     "Channel 6 · Sat 10 Oct, 20:00–24:00 · average utilization 40 % (peak 96 %) · 3 APs reporting · unusual spike");
  eq([null, 0, 9.9, 10, 64.9, 65].map((avg) => utilColor({ avg })),
     ["var(--hatch)", "var(--ramp-1)", "var(--ramp-1)", "var(--ramp-2)", "var(--ramp-6)", "var(--ramp-7)"]);
});

test("utilization ticks", () => {
  eq(utilTicks("day", "7d", [], TZ).map((x) => x.t), ["00:00", "06:00", "12:00", "18:00"]);
  const hours = (start, n, step = 1) => Array.from({ length: n }, (_, i) => ({
    start: new Date(Date.parse(start) + i * step * 3600e3).toISOString() }));
  // 24 h from 13:00 local: the first cell, then every 6 hours
  eq(utilTicks("timeline", "24h", hours("2026-10-11T11:00:00Z", 24), TZ).map((x) => x.t),
     ["Sun 13:00", "18:00", "Mon 00:00", "06:00", "12:00"]);
  // 7 days from Mon 18:00 local: each midnight
  eq(utilTicks("timeline", "7d", hours("2026-10-05T16:00:00Z", 168), TZ).map((x) => x.t),
     ["Tue 6 Oct", "Wed 7 Oct", "Thu 8 Oct", "Fri 9 Oct", "Sat 10 Oct", "Sun 11 Oct", "Mon 12 Oct"]);
  // 30 days of 4-hour blocks from Sun 13 Sep 00:00 local: the first day, then Mondays (the next day
  // skipped, Mon 12 Oct at 97 % too close to the right edge)
  eq(utilTicks("timeline", "30d", hours("2026-09-12T22:00:00Z", 180, 4), TZ).map((x) => x.t),
     ["Sun 13 Sep", "Mon 21 Sep", "Mon 28 Sep", "Mon 5 Oct"]);
});

done();
