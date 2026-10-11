import { test, eq, done } from "../testing/tiny.js";
import { bucketLabel, niceMax, pct, tipStyle, xLabels } from "./geometry.js";

const TZ = "Europe/Bratislava";

function hours(startIso, n, step) {
  const out = [];
  let t = Date.parse(startIso);
  for (let i = 0; i < n; i += 1) {
    out.push({ start: new Date(t).toISOString(), end: new Date(t + step * 3600e3).toISOString() });
    t += step * 3600e3;
  }
  return out;
}

test("y max in 50s", () => {
  eq(niceMax(0), 50);
  eq(niceMax(141), 150);
  eq(niceMax(150), 150);
  eq(niceMax(7, 5), 10);
});

test("tooltip beside the column", () => {
  eq(tipStyle(0, 28), { left: "calc(3.57% + 6px)" });
  eq(tipStyle(27, 28), { right: "calc(3.57% + 6px)" });
});

test("bucket labels as the boards", () => {
  const b = { start: "2026-10-10T10:00:00Z", end: "2026-10-10T16:00:00Z" };
  eq(bucketLabel(b, "6h", "2026-10-10T15:52:00Z", TZ), "Sat 10 Oct, 12:00–17:52");   // the current window ends now
  eq(bucketLabel({ start: "2026-10-09T16:00:00Z", end: "2026-10-09T22:00:00Z" }, "6h", "2026-10-10T15:52:00Z", TZ),
     "Fri 9 Oct, 18:00–24:00");
  eq(bucketLabel({ start: "2026-10-09T22:00:00Z", end: "2026-10-10T22:00:00Z" }, "1d", "2026-10-10T15:52:00Z", TZ),
     "Sat 10 Oct");
  eq(bucketLabel({ start: "2026-10-10T14:00:00Z", end: "2026-10-10T15:00:00Z" }, "1h", "2026-10-10T15:52:00Z", TZ),
     "Sat 10 Oct, 16:00–17:00");
});

test("x labels at day / 6-hour marks", () => {
  const h = hours("2026-10-09T16:00:00Z", 24, 1);                  // Fri 18:00 .. Sat 17:00 local
  eq(xLabels(h, "1h", TZ).map((x) => x.t), ["Fri 18:00", "Sat 00:00", "06:00", "12:00"]);
  const w = hours("2026-10-03T16:00:00Z", 28, 6);                  // 3 Oct 18:00 local, 6-hour windows
  const lw = xLabels(w, "6h", TZ);
  eq(lw.map((x) => x.t).slice(0, 2), ["Sun 4 Oct", "Mon 5 Oct"]);
  eq(lw[0].left, "3.57%");
  const d = hours("2026-09-10T22:00:00Z", 30, 24);                 // 11 Sep .. 10 Oct
  eq(xLabels(d, "1d", TZ).map((x) => x.t), ["Fri 11 Sep", "Mon 14 Sep", "Mon 21 Sep", "Mon 28 Sep", "Mon 5 Oct"]);
});

test("bar heights", () => {
  eq(pct(25, 50), "50.0%");
  eq(pct(null, 50), "0.0%");
});

done();
