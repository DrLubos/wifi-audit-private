// Geometry and labels shared by the charts: the y-axis maximum, the tooltip
// side, bucket labels and the x-axis ticks - as the boards compute them, but
// from the api's bucket boundaries in the account's time zone. Pure, tested
// in geometry.test.js.

import { fmtDay, fmtTime, partsOf, toDate } from "../format.js";

// the y-axis top: the maximum rounded up to the step (boards: 50s), at least one step
export function niceMax(max, step = 50) {
  return Math.max(step, Math.ceil((max || 0) / step) * step);
}

// tooltip beside column i of n, never over it: left half -> right of the column
export function tipStyle(i, n) {
  return i < n / 2
    ? { left: `calc(${(((i + 1) / n) * 100).toFixed(2)}% + 6px)` }
    : { right: `calc(${(((n - i) / n) * 100).toFixed(2)}% + 6px)` };
}

export const UNIT = { "1h": "hour", "6h": "6-hour window", "1d": "day" };

// "Sat 10 Oct" (days) or "Sat 10 Oct, 12:00–18:00"; the current bucket ends now
export function bucketLabel(b, bucket, now, tz) {
  if (bucket === "1d") return fmtDay(b.start, tz);
  const end = toDate(b.end) > toDate(now) ? fmtTime(now, tz) : fmtTime(b.end, tz);
  return `${fmtDay(b.start, tz)}, ${fmtTime(b.start, tz)}–${bucket === "6h" && end === "00:00" ? "24:00" : end}`;
}

// x-axis labels at the buckets where a day (or a 6-hour mark) starts:
// [{t, left}] with left in % of the plot width
export function xLabels(buckets, bucket, tz) {
  const out = [];
  const n = buckets.length;
  buckets.forEach((b, i) => {
    const p = partsOf(b.start, tz);
    const left = `${((i / n) * 100).toFixed(2)}%`;
    const h = Number(p.hour);
    if (bucket === "1h") {
      if (i === 0) out.push({ t: `${p.weekday} ${fmtTime(b.start, tz)}`, left });
      else if (h % 6 === 0) out.push({ t: h === 0 ? `${p.weekday} 00:00` : fmtTime(b.start, tz), left });
    } else if (bucket === "6h") {
      if (h === 0) out.push({ t: fmtDay(b.start, tz), left });
    } else if (i === 0 || p.weekday === "Mon") {
      out.push({ t: fmtDay(b.start, tz), left });
    }
  });
  return out;
}

// drop x labels that would sit closer than minGap px to the previous one kept
// (narrow screens); a label too close to the right edge is aligned to it
// (alignRight) instead of running past it. width 0 = not measured yet.
export function thinLabels(labels, width, minGap = 76) {
  if (!width) return labels;
  const out = [];
  let last = -Infinity;
  for (const x of labels) {
    const px = (parseFloat(x.left) / 100) * width;
    const at = Math.min(px, width - minGap);
    if (at - last >= minGap) {
      out.push(at < px ? { ...x, alignRight: true } : x);
      last = at;
    }
  }
  return out;
}

// a bar's height in % of the plot (0 stays 0)
export function pct(v, top) {
  return `${((Math.max(0, v || 0) / top) * 100).toFixed(1)}%`;
}
