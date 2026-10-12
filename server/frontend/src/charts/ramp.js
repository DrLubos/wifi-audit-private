// The sequential blue ramp of the heatmaps (PLAN.md 8.4): seven colors, a
// value's color by six thresholds (the clients heatmap: from the account's
// 30-day peak; utilization: the Spectrum board's fixed percentages), the
// legend's labels. Pure, tested in ramp.test.js.

import { fmtInt } from "../format.js";

export const RAMP_COLORS = ["var(--ramp-1)", "var(--ramp-2)", "var(--ramp-3)", "var(--ramp-4)", "var(--ramp-5)",
                            "var(--ramp-6)", "var(--ramp-7)"];

// the ramp step of a value (six thresholds, e.g. 10 30 70 110 160 200)
export function rampIndex(v, ramp) {
  let i = 0;
  while (i < ramp.length && v >= ramp[i]) i += 1;
  return i;              // 0 .. ramp.length (7 colors)
}

// the legend: the lower bound of each color, the last one open ("0 10 30 ... 200+")
export function rampLabels(ramp, unit = "") {
  return [0, ...ramp].map((t, i) => `${fmtInt(t)}${unit}${i === ramp.length ? "+" : ""}`);
}
