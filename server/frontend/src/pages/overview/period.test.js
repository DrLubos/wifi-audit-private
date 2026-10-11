import { test, eq, done } from "../../testing/tiny.js";
import { chartSub, collecting, columnNotes, rangeText, tileTexts } from "./period.js";

const TZ = "Europe/Bratislava";

const d = {
  period: { from: "2026-10-03T16:00:00Z", bucket: "6h" },
  buckets: [
    { start: "2026-10-06T10:00:00Z", end: "2026-10-06T16:00:00Z", coverage: { "pi-fri": 1.0, "pi-lab": 0.4 } },
    { start: "2026-10-06T16:00:00Z", end: "2026-10-06T22:00:00Z", coverage: { "pi-fri": 0.582, "pi-lab": 0 } },
    { start: "2026-10-06T22:00:00Z", end: "2026-10-07T04:00:00Z", coverage: { "pi-fri": 0, "pi-lab": 0 } },
  ],
  aps: {
    fixed: [120, 98, null], mobile: [21, 7, null],
    events: [[{ severity: "high", title: "Security WPA2-PSK → Open" }], [], []], event_counts: [3, 0, 0],
  },
  tiles: {
    new_aps: { total: 159, mobile_share: 0.975 },
    open_nearby: { total: 40, aps_seen: 416, share: 0.096 },
    clients_managed: { peak: 125, peak_at: "2026-09-30T22:00:00Z", avg_label: "average 6-hour peak", avg: 82.1 },
  },
};
const sensors = [{ name: "pi-fri", first_poll: "2026-09-15T13:53:07Z" }, { name: "pi-lab", first_poll: "2026-10-06T12:00:00Z" }];

test("range text: start of the period, the viewer's now", () => {
  eq(rangeText("7d", "2026-10-03T16:00:00Z", "2026-10-10T15:52:00Z", TZ), "last 7 days: 3 Oct 18:00 – 10 Oct 17:52");
  eq(rangeText("30d", "2026-09-10T22:00:00Z", "2026-10-10T15:52:00Z", TZ), "last 30 days: 11 Sep – 10 Oct 17:52");
});

test("collecting buckets", () => {
  eq(collecting(d.buckets), [true, true, false]);
});

test("column notes: coverage, events, no data", () => {
  eq(columnNotes(d, 0, "all", sensors),
     ["pi-lab collected 40 % of this window", "Event · high · Security WPA2-PSK → Open", "+ 2 more events"]);
  eq(columnNotes(d, 1, "all", sensors), ["pi-fri collected 58 % of this window", "pi-lab was not collecting"]);
  eq(columnNotes(d, 2, "all", sensors), ["No sensor was collecting in this window"]);
  eq(columnNotes(d, 1, "pi-fri", sensors), ["pi-fri collected 58 % of this window"]);
});

test("chart sub-line", () => {
  eq(chartSub(d, ["a", "b", "c"], [true, true, false]),
     "APs per 6-hour window · peak 141 (a) · latest window no data");
});

test("tile texts", () => {
  const t = tileTexts(d, TZ);
  eq(t.newSub, "98 % of them mobile hotspots · first seen in this period");
  eq(t.openSub, "10 % of 416 APs seen in the period");
  eq(t.clientsSub, "average 6-hour peak 82 · peak Thu 1 Oct 00:00");
});

done();
