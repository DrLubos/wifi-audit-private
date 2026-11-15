import { test, eq, done } from "./testing/tiny.js";
import { fmtAgo, fmtDateTime, fmtDay, fmtDbm, fmtDuration, fmtInt, fmtPct, fmtTime, partsOf, sameDay } from "./format.js";

const TZ = "Europe/Bratislava";

test("local date and time, 24-hour clock", () => {
  eq(fmtDateTime("2026-10-10T15:52:00Z", TZ), "10 Oct 17:52");
  eq(fmtDay("2026-10-10T15:52:00Z", TZ), "Sat 10 Oct");
  eq(fmtTime("2026-10-10T22:00:00Z", TZ), "00:00");                // midnight, not 24:00
  eq(fmtDateTime("2026-09-15T13:53:07Z", TZ), "15 Sep 15:53");     // "Sep", not "Sept"
  eq(fmtDateTime("2026-10-25T01:30:00Z", TZ), "25 Oct 02:30");     // after the DST change (CET)
  eq(fmtDateTime(null, TZ), "–");
  eq(partsOf("2026-10-10T15:52:00Z", "UTC").hour, "15");
});

test("durations and ages", () => {
  eq(fmtDuration(45), "45 s");
  eq(fmtDuration(361), "6 min");
  eq(fmtDuration(3 * 3600 + 59 * 60), "3 h");
  eq(fmtDuration(433917), "5 d");
  eq(fmtAgo("2026-10-10T15:51:48Z", "2026-10-10T15:52:00Z"), "12 s ago");
  eq(fmtAgo("2026-10-10T15:51:58Z", "2026-10-10T15:52:00Z"), "now");
});

test("numbers", () => {
  eq(fmtInt(1234567), "1,234,567");
  eq(fmtInt(null), "–");
  eq(fmtPct(0.0938), "9 %");
  eq(fmtPct(0.0938, 1), "9.4 %");
  eq(fmtDbm(-63.4), "−63");
});

test("the same local day is day, month and year", () => {
  const tz = "Europe/Bratislava";
  eq(sameDay("2026-10-06T19:33:15Z", "2026-10-06T21:50:00Z", tz), true);
  eq(sameDay("2026-10-06T19:33:15Z", "2026-10-06T22:10:00Z", tz), false);     // 00:10 local on the 7th
  eq(sameDay("2026-10-06T19:33:15Z", "2026-11-06T08:10:00Z", tz), false);     // the 6th of another month
  eq(sameDay("2026-10-06T19:33:15Z", null, tz), false);
});

done();
