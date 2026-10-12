// The texts and shapes of the Spectrum page (boards/Spectrum.dc.html) from
// the api's rows - pure, tested in spectrum.test.js.

import { RAMP_COLORS, rampIndex } from "../../charts/ramp.js";
import { fmtDay, fmtInt, fmtTime, partsOf } from "../../format.js";

// utilization in % per color (the board's fixed scale: a share of airtime
// compares across channels, periods and sites)
export const UTIL_RAMP = [10, 20, 30, 40, 50, 65];

export function bandName(band) {
  return band === "5" ? "5 GHz" : "2.4 GHz";
}

// utilization 12.94 -> "13 %"
export function utilText(v) {
  return v == null ? "–" : `${Math.round(v)} %`;
}

// an interquartile range [p25, p75] -> "8–10" (one number when both round alike)
export function rangeOf(iqr) {
  if (!iqr) return null;
  const [a, b] = iqr.map((v) => Math.round(v));
  return a === b ? String(a) : `${a}–${b}`;
}

// a "good" channel: 2.4 GHz 1 / 6 / 11 (no overlap), 5 GHz outside DFS
export function goodChannel(c, band) {
  return band === "5" ? !c.dfs : !c.overlaps;
}

// The occupancy columns: every 2.4 GHz channel (an empty one says something),
// the 5 GHz channels with APs (25 channels would leave no room, as the board).
export function occupancy(channels, band) {
  const shown = band === "5" ? channels.filter((c) => c.aps > 0) : channels;
  const max = Math.max(1, ...shown.map((c) => c.aps));
  return shown.map((c) => {
    const good = goodChannel(c, band);
    const fill = good ? "var(--c1)" : "var(--c2)";
    return {
      ch: c.channel,
      n: c.aps,
      good,
      fill,
      h: c.aps ? `${Math.max(2, Math.round((c.aps / max) * 82))}%` : "0%",
      aria: `Channel ${c.channel}: ${fmtInt(c.aps)} APs`
        + (c.util_avg != null ? `, average utilization ${utilText(c.util_avg)}` : ""),
      head: `Channel ${c.channel} · ${bandName(band)}`,
      rows: [{ c: fill, v: fmtInt(c.aps), l: "APs in the period" },
             ...(c.util_avg != null ? [{ c: "transparent", v: utilText(c.util_avg), l: "average utilization" }] : [])],
      notes: [band === "5"
        ? (good ? "Non-DFS channel" : "DFS: the AP must leave the channel when it detects radar")
        : (good ? "Does not overlap with 1 / 6 / 11" : "Overlaps with neighbouring channels")],
    };
  });
}

export const LEGEND = {
  "2.4": ["channels 1, 6, 11 (do not overlap)", "overlapping channels"],
  5: ["non-DFS", "DFS (52–144)"],
};

export function occNote(band, currentChannelAps) {
  const base = band === "5"
    ? "5 GHz channels do not overlap. On DFS channels the AP must change channel when it detects radar."
    : "2.4 GHz channels are only 5 MHz apart but each is about 20 MHz wide. Without overlap, 1, 6 and 11 fit "
      + "side by side (25 MHz apart). Europe allows channels 1–13, so pure 20 MHz OFDM could also use "
      + "1/5/9/13, but US devices only see 1–11 and vendors default to 1/6/11.";
  // APs without a load row in the period: no polls per channel, so their current channel
  const cur = currentChannelAps
    ? ` Each AP counts once, on the channel it used most in the period; ${fmtInt(currentChannelAps)}`
      + ` ${currentChannelAps === 1 ? "AP" : "APs"} without a load record in the period on the current channel.`
    : " Each AP counts once, on the channel it used most in the period.";
  return base + cur;
}

const hiddenKey = (k) => k.startsWith("hidden:");

const nets = (n, what) => `${fmtInt(n)} ${what}${n === 1 ? "network" : "networks"}`;

// "UZVD-zamestnanci 7–11×, IK-WIFI-DOT1X 3–9× per day, 1 more network and 9 hidden networks"
export function dynamicText(changes, shown = 3) {
  const named = changes.filter((c) => !hiddenKey(c.network));
  const hidden = changes.length - named.length;
  const parts = named.slice(0, shown).map((c) => `${c.network} ${rangeOf(c.per_day)}×`);
  const more = named.length - parts.length;
  const tail = [more > 0 ? nets(more, "more ") : null, hidden ? nets(hidden, "hidden ") : null].filter(Boolean);
  if (!parts.length) return tail.join(" and ");
  if (!tail.length) return `${parts.join(", ")} per day`;
  return `${parts.join(", ")} per day${tail.length === 2 ? `, ${tail[0]} and ${tail[1]}` : ` and ${tail[0]}`}`;
}

// "Channel plan issues" (board): overlap, 40 MHz in 2.4 GHz, the busiest
// channel of the band, dynamic channel selection, 1/6/11 vs 1/5/9/13
export function issueItems(issues, band) {
  const items = [
    { cls: issues.overlapping ? "warn" : "ok", badge: `${fmtInt(issues.overlapping)} APs`,
      text: "on overlapping 2.4 GHz channels (outside 1, 6, 11) – they interfere with each other" },
    { cls: issues.wide_2g ? "warn" : "ok", badge: `${fmtInt(issues.wide_2g)} APs`,
      text: "use 40 MHz width in 2.4 GHz, where only three independent 20 MHz channels fit" },
  ];
  const b = issues.busiest;
  if (b) {
    items.push({ cls: "neu", badge: `Channel ${b.channel}`,
                 text: `busiest in ${bandName(band)}: ${fmtInt(b.aps)} APs, `
                   + (b.day_util ? `daytime utilization ${rangeOf(b.day_util)} %` : "no daytime utilization reported") });
  }
  const dyn = issues.channel_changes ?? [];
  items.push(dyn.length
    ? { cls: "info", badge: `${fmtInt(dyn.length)} ${dyn.length === 1 ? "network" : "networks"}`,
        text: `change channel 4 or more times a day (dynamic channel selection): ${dynamicText(dyn)}` }
    : { cls: "neu", badge: "Stable", text: "no network changes channel 4 or more times a day" });
  const p = issues.plan;
  items.push({ cls: "neu", badge: "1/5/9/13 plan",
               text: p.ch_5_9_13 * 4 < p.ch_1_6_11
                 ? `not used nearby: channels 5, 9 and 13 have ${fmtInt(p.ch_5_9_13)} APs together, channels 1, 6 `
                   + `and 11 have ${fmtInt(p.ch_1_6_11)}`
                 : `in use nearby too: channels 5, 9 and 13 have ${fmtInt(p.ch_5_9_13)} APs together, channels 1, 6 `
                   + `and 11 have ${fmtInt(p.ch_1_6_11)}` });
  return items;
}

const two = (n) => String(n).padStart(2, "0");

export function timelineLabel(range) {
  return range === "24h" ? "Timeline (24 × 1 h)" : range === "7d" ? "Timeline (7 × 24 h)"
    : "Timeline (30 days, 4-hour blocks)";
}

export function utilSub(view, range) {
  if (view === "day") return "Average for each hour of the day over the selected period.";
  return range === "30d"
    ? "Each cell is the average of a 4-hour block, so 30 days stay readable; hover shows the average and the peak."
    : "Each cell is one hour of the period: repeating patterns and one-off events are both visible.";
}

// the hint of one cell ("Channel 6 · Mon 12 Oct, 09:00–10:00 · utilization 13 % · 4 APs reporting")
export function utilCellText(view, ch, cell, tz, now) {
  const reporting = cell.aps ? ` · ${fmtInt(cell.aps)} ${cell.aps === 1 ? "AP" : "APs"} reporting` : "";
  if (view === "day") {
    const head = `Channel ${ch} · ${two(cell.hour)}:00–${two((cell.hour + 1) % 24)}:00`;
    return cell.avg == null ? `${head} · no utilization reported`
      : `${head} · average utilization ${utilText(cell.avg)}, highest hour ${utilText(cell.peak)}${reporting}`;
  }
  const block = new Date(cell.end) - new Date(cell.start) > 3600e3;
  const end = fmtTime(cell.end, tz);
  const head = `Channel ${ch} · ${fmtDay(cell.start, tz)}, ${fmtTime(cell.start, tz)}–${block && end === "00:00" ? "24:00" : end}`;
  if (cell.avg == null) {
    if (new Date(cell.start) >= new Date(now)) return `${head} · not yet`;
    return `${head} · ${cell.captured ? "no AP reported utilization" : "no sensor collecting"}`;
  }
  const spike = cell.peak >= 90 ? " · unusual spike" : "";
  return block
    ? `${head} · average utilization ${utilText(cell.avg)} (peak ${utilText(cell.peak)})${reporting}${spike}`
    : `${head} · utilization ${utilText(cell.avg)}${reporting}${spike}`;
}

export function utilColor(cell) {
  return cell.avg == null ? "var(--hatch)" : RAMP_COLORS[rampIndex(cell.avg, UTIL_RAMP)];
}

// x-axis ticks [{t, left}]: the day view at 00/06/12/18; the timeline at the
// local midnights (7 days), Mondays and the first day (30 days), the first
// cell and every 6 hours (24 h)
export function utilTicks(view, range, cells, tz) {
  if (view === "day") return [0, 6, 12, 18].map((h) => ({ t: `${two(h)}:00`, left: `${((h / 24) * 100).toFixed(2)}%` }));
  const n = cells.length;
  const out = [];
  cells.forEach((c, i) => {
    const p = partsOf(c.start, tz);
    if (!p) return;
    const h = Number(p.hour);
    const left = `${((i / n) * 100).toFixed(2)}%`;
    if (range === "24h") {
      if (i === 0) out.push({ t: `${p.weekday} ${fmtTime(c.start, tz)}`, left });
      else if (h % 6 === 0) out.push({ t: h === 0 ? `${p.weekday} 00:00` : fmtTime(c.start, tz), left });
    } else if (range === "7d") {
      if (h === 0) out.push({ t: fmtDay(c.start, tz), left });
    } else if (h === 0 && (out.length === 0 || p.weekday === "Mon")
               && (out.length === 0 || i / n - parseFloat(out[out.length - 1].left) / 100 >= 0.05)) {
      out.push({ t: fmtDay(c.start, tz), left });          // a Monday 1-2 days after the first day: skipped
    }
  });
  // a day label ("Mon 12 Oct") in the last 7 % would run past the right edge (no measuring here)
  return range === "24h" ? out : out.filter((x, i) => i === 0 || parseFloat(x.left) <= 93);
}
