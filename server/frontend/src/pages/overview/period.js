// The texts of the "Selected period" block (Main board), from the period
// response - pure, tested in period.test.js.

import { bucketLabel, UNIT } from "../../charts/geometry.js";
import { fmtDate, fmtDateTime, fmtDay, fmtInt, fmtPct, fmtTime, toDate } from "../../format.js";

const TITLE = { "24h": "last 24 h", "7d": "last 7 days", "30d": "last 30 days" };

// "last 7 days: 3 Oct 18:00 – 10 Oct 17:52" (the end is the viewer's now)
export function rangeText(range, from, now, tz) {
  const start = range === "30d" ? fmtDate(from, tz) : fmtDateTime(from, tz);
  return `${TITLE[range] ?? range}: ${start} – ${fmtDateTime(now, tz)}`;
}

// a bucket counts as collected when any selected sensor collected in it
export function collecting(buckets) {
  return buckets.map((b) => Object.values(b.coverage).some((c) => c > 0));
}

export function labelsOf(d, now, tz) {
  return d.buckets.map((b) => bucketLabel(b, d.period.bucket, now, tz));
}

// The tooltip notes of an "APs over time" column: partial coverage per
// sensor (a sensor not installed yet is not "not collecting"), then the
// bucket's events.
export function columnNotes(d, i, sensorFilter, sensors) {
  const b = d.buckets[i];
  const names = Object.keys(b.coverage);
  const on = names.some((n) => b.coverage[n] > 0);
  const notes = on ? [] : ["No sensor was collecting in this window"];
  if (on) {
    for (const n of names) {
      const c = b.coverage[n];
      if (c > 0 && c < 0.995) notes.push(`${n} collected ${Math.round(c * 100)} % of this window`);
      else if (c === 0 && sensorFilter === "all") {
        const s = sensors?.find((x) => x.name === n);
        if (!s?.first_poll || toDate(s.first_poll) < toDate(b.end)) notes.push(`${n} was not collecting`);
      }
    }
  }
  for (const e of d.aps.events[i] ?? []) notes.push(`Event · ${e.severity} · ${e.title}`);
  const more = (d.aps.event_counts[i] ?? 0) - (d.aps.events[i]?.length ?? 0);
  if (more > 0) notes.push(`+ ${more} more ${more === 1 ? "event" : "events"}`);
  return notes;
}

// "APs per 6-hour window · peak 141 (Thu 1 Oct, 12:00–18:00) · latest window 98, of which 7 mobile"
export function chartSub(d, labels, on) {
  const unit = UNIT[d.period.bucket];
  const tot = d.aps.fixed.map((f, i) => (on[i] ? (f ?? 0) + (d.aps.mobile[i] ?? 0) : null));
  let peak = -1;
  tot.forEach((t, i) => { if (t != null && (peak < 0 || t > tot[peak])) peak = i; });
  const last = tot.length - 1;
  const parts = [`APs per ${unit}`];
  if (peak >= 0) parts.push(`peak ${fmtInt(tot[peak])} (${labels[peak]})`);
  parts.push(tot[last] != null
    ? `latest window ${fmtInt(tot[last])}, of which ${fmtInt(d.aps.mobile[last] ?? 0)} mobile`
    : "latest window no data");
  return parts.join(" · ");
}

// the three tiles' texts
export function tileTexts(d, tz) {
  const t = d.tiles;
  const c = t.clients_managed;
  return {
    newSub: t.new_aps.total
      ? `${fmtPct(t.new_aps.mobile_share)} of them mobile hotspots · first seen in this period`
      : "none first seen in this period",
    openSub: t.open_nearby.aps_seen
      ? `${fmtPct(t.open_nearby.share)} of ${fmtInt(t.open_nearby.aps_seen)} APs seen in the period`
      : "no APs seen in the period",
    clientsSub: c.peak == null
      ? "client counts were not recorded in this period"
      : `${c.avg_label} ${fmtInt(c.avg)} · peak ${fmtDay(c.peak_at, tz)} ${fmtTime(c.peak_at, tz)}`,
  };
}
