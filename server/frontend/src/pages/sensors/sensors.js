// The texts of the Sensors page (boards/Sensors.dc.html) from
// /api/v2/sensors, /sensors/{name}/coverage and /outages - pure, tested in
// sensors.test.js. Until live ingest a sensor's data ends at its last import
// (SPEC Q7): "between imports" is neither online nor offline.

import { sensorState } from "../../app/status.js";
import { fmtAgo, fmtDate, fmtDateTime, fmtDay, fmtInt, fmtTime, partsOf, toDate } from "../../format.js";

const HOUR = 3600e3;

// "45 s", "6 min", "5 h 21 min", "4 d 2 h" - two units from an hour on
export function durText(seconds) {
  if (seconds == null) return "–";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) {
    const m = Math.floor((s % 3600) / 60);
    return `${Math.floor(s / 3600)} h${m ? ` ${m} min` : ""}`;
  }
  const h = Math.floor((s % 86400) / 3600);
  return `${Math.floor(s / 86400)} d${h ? ` ${h} h` : ""}`;
}

const pct1 = (share) => `${(Math.round(share * 1000) / 10).toFixed(1)} %`;

// "1 sensor in the account · 0 online · 1 between imports"
export function headText(sensors, offlineMin, now, tz) {
  const st = sensors.map((s) => sensorState(s, offlineMin, now, tz));
  const online = st.filter((x) => x.tone === "ok").length;
  const offline = st.filter((x) => x.offline).length;
  const between = sensors.filter((s, i) => st[i].tone === "neu" && toDate(s.last_poll)).length;
  return [`${fmtInt(sensors.length)} ${sensors.length === 1 ? "sensor" : "sensors"} in the account`,
          `${fmtInt(online)} online`, offline ? `${fmtInt(offline)} offline` : null,
          between ? `${fmtInt(between)} between imports` : null].filter(Boolean).join(" · ");
}

// "12 s ago (every 30 s)" within the hour, else "11 Oct 22:46 (every 30 s)"
export function lastPollText(s, now, tz) {
  const t = toDate(s.last_poll);
  if (!t) return "no poll yet";
  const when = now - t.getTime() < HOUR ? fmtAgo(t, now) : fmtDateTime(t, tz);
  return s.poll_s ? `${when} (every ${fmtInt(s.poll_s)} s)` : when;
}

// "11 Oct 22:50 · snapshot import, data to 22:46"; ingest later: "12 s ago · after every poll"
export function uploadText(u, now, tz) {
  if (!u) return "none yet";
  if (u.via === "ingest") return `${fmtAgo(u.at, now)} · after every poll`;
  const same = u.polls_to && partsOf(u.polls_to, tz).day === partsOf(u.at, tz).day;
  const to = u.polls_to ? `, data to ${same ? fmtTime(u.polls_to, tz) : fmtDateTime(u.polls_to, tz)}` : "";
  return `${fmtDateTime(u.at, tz)} · snapshot import${to}`;
}

// "604 MB · 14 days" (at the last import)
export function bufferText(u) {
  if (!u || u.buffer_bytes == null) return "unknown";
  const mb = `${fmtInt(u.buffer_bytes / 1e6)} MB`;
  const from = toDate(u.polls_from);
  const to = toDate(u.polls_to);
  if (!from || !to) return mb;
  return `${mb} · ${fmtInt((to - from) / (24 * HOUR))} days`;
}

// "40.8 % of the time collecting"
export function coverageText(s) {
  return s.coverage_7d == null ? "–" : `${pct1(s.coverage_7d)} of the time collecting`;
}
export function coverageTitle(s, tz) {
  return s.window_7d ? `${fmtDateTime(s.window_7d.from, tz)} – ${fmtDateTime(s.window_7d.to, tz)}, to the last poll` : undefined;
}

// "2 (each recovered within 7 s)", "3 (2 within 1 min, the longest after 4 d 2 h)", "none"
export function crashText(c) {
  if (!c || !c.count) return "none";
  const n = fmtInt(c.count);
  if (c.within_60s === c.count) return `${n} (each recovered within ${durText(c.max_recovery_s)})`;
  const quick = c.within_60s ? `${fmtInt(c.within_60s)} within 1 min, ` : "";
  return `${n} (${quick}the longest after ${durText(c.max_recovery_s)})`;
}
// re-opens without an error before them: restarts, not crashes
export function restartText(c) {
  if (!c?.restarts) return null;
  return `${fmtInt(c.restarts)} ${c.restarts === 1 ? "restart" : "restarts"} without a crash`;
}

// "316 · 241 of them only by this sensor"; one sensor in the account: "316"
export function apsText(s, nSensors) {
  const all = fmtInt(s.aps_7d);
  return nSensors > 1 ? `${all} · ${fmtInt(s.aps_only_here_7d)} of them only by this sensor` : all;
}

export const PLANS = {
  all_89: { label: "All channels and widths", note: "" },
  ht20_37: { label: "20 MHz only", note: " (20 MHz only)" },
  locked: { label: "Locked to one channel", note: " (locked)" },
  custom: { label: "Custom list", note: " (custom list)" },
};

// "89 channels · 0.2 s dwell · poll 30 s"
export function configText(c) {
  if (!c) return "not recorded";
  const n = c.n_channels ?? c.channels?.length ?? 0;
  const plan = PLANS[c.plan]?.note ?? "";
  return `${fmtInt(n)} ${n === 1 ? "channel" : "channels"}${plan} · ${c.dwell_s} s dwell · poll ${fmtInt(c.poll_s)} s`;
}

// "Channels 89/89" from the last collecting poll; warn when the sensor
// visits fewer entries than its list has
export function hopBadge(s) {
  if (!s.hop) return null;
  const ok = s.hop.ok && s.hop.visited === s.hop.n;
  return { cls: ok ? "ok" : "warn", text: `Channels ${fmtInt(s.hop.visited)}/${fmtInt(s.hop.n)}`,
           title: "hop entries visited / in the list, at the last collecting poll" };
}

// the card's foot: a pending change (not delivered until the sensor upload
// exists, Q10) or since when the configuration is in effect
export function footText(s, tz) {
  if (s.pending) {
    return { warn: true, text: `Pending change: ${configText(s.pending)} - not delivered, the sensor upload is not built yet` };
  }
  if (s.config?.confirmed_at) return { warn: false, text: `Configuration in effect since ${fmtDateTime(s.config.confirmed_at, tz)}` };
  return { warn: false, text: "No configuration recorded" };
}

// the box under the details: offline (the data ends in an outage) or
// between imports
export function stateNote(s, st, tz) {
  if (st.offline) {
    const since = s.outage_since ?? s.last_capture_at;
    return { bad: true, text: `Not collecting since ${since ? fmtDateTime(since, tz) : "its first poll"}. `
      + "Check the capture adapter, power and network; the buffer keeps 14 days." };
  }
  if (st.tone === "neu" && s.last_poll) {
    return { bad: false, text: `The data ends at the last import (${fmtDateTime(s.last_upload?.at ?? s.last_poll, tz)}); `
      + "live upload from the sensor is not built yet, so it reads neither online nor offline." };
  }
  return null;
}

// the capture adapter (read-only, schema 13) and the last hardware change entered by hand:
// "RTL8821CU USB adapter · 0bda:c811 · last hardware change 13 Oct 14:00"
export function adapterText(a, lastChange, tz) {
  if (!a) return "not recorded";
  const id = a.usb_id ?? "VID:PID not reported yet";
  return `${a.name} · ${id} · ${lastChange ? `last hardware change ${fmtDateTime(lastChange, tz)}` : "no hardware change recorded"}`;
}

// "since 15 Sep 2026"
export function sinceText(s, tz) {
  if (!s.first_poll) return null;
  return `since ${fmtDate(s.first_poll, tz)} ${partsOf(s.first_poll, tz).year}`;
}

// The coverage columns, top to bottom (boards/Sensors.dc.html + the 5th
// state "no data yet" after the last import, PLAN 3.7).
export const COVERAGE = [
  { key: "no_data", label: "no data yet (after the last import)", color: "var(--wash)" },
  { key: "outage", label: "outage", color: "var(--dot-bad)" },
  { key: "limited", label: "limited channels", color: "var(--lvl-psk)" },
  { key: "ok", label: "successful polls, full channel list", color: "var(--dot-ok)" },
  { key: "not_installed", label: "not installed yet", color: "var(--hatch)", hatch: true },
];

// the legend in the board's order, the states that occur in the days (successful always)
const LEGEND = ["ok", "limited", "outage", "not_installed", "no_data"];
export function legendItems(days) {
  return LEGEND.map((k) => COVERAGE.find((c) => c.key === k))
    .filter((c) => c.key === "ok" || (days ?? []).some((d) => d[c.key] > 0));
}

const LOCAL_DAY = (date) => new Date(`${date}T12:00:00Z`);     // a date's noon: its label in every zone

const ymd = new Map();
// "2026-10-06": the local date of an instant
export function localDate(v, tz) {
  let f = ymd.get(tz);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" });
    ymd.set(tz, f);
  }
  const p = Object.fromEntries(f.formatToParts(toDate(v)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

// the documented causes of the outages and limited periods that overlap a local day
export function dayNotes(date, outages, tz) {
  const out = [];
  for (const o of outages ?? []) {
    if (!o.cause) continue;
    const a = localDate(o.from, tz);
    const b = o.to ? localDate(o.to, tz) : "9999-12-31";
    if (a <= date && date <= b && !out.includes(o.cause)) out.push(o.cause);
  }
  return out;
}

// [{label, head, segs: [{key, share, color, hatch}], rows, notes, aria}] - one per day
export function coverageColumns(days, sensor, outages, tz, today) {
  return days.map((d) => {
    const at = LOCAL_DAY(d.date);
    const label = fmtDate(at, "UTC");
    const segs = COVERAGE.filter((c) => d[c.key] > 0).map((c) => ({ ...c, share: d[c.key] }));
    const rows = COVERAGE.filter((c) => d[c.key] > 0 || c.key === "ok")
      .map((c) => ({ c: c.hatch ? "var(--control)" : c.color, v: pct1(d[c.key]), l: c.label }));
    const head = `${sensor} · ${fmtDay(at, "UTC")}${d.date === today ? " (today)" : ""}`;
    return {
      date: d.date, label, head, segs, rows, notes: dayNotes(d.date, outages, tz),
      aria: `${fmtDay(at, "UTC")}: ${rows.map((r) => `${r.v} ${r.l}`).join(", ")}`,
    };
  });
}

export const OUTAGE_TYPES = {
  outage: { cls: "bad", text: "Outage" },
  limited: { cls: "warn", text: "Limited channels" },
  restart: { cls: "neu", text: "Restart" },
};

// the table rows: from, duration (ongoing), type badge, cause
export function outageRows(items, tz) {
  return (items ?? []).map((o) => ({
    key: `${o.type}|${o.from}`,
    from: fmtDateTime(o.from, tz),
    title: o.to ? `${fmtDateTime(o.from, tz)} – ${fmtDateTime(o.to, tz)}` : `since ${fmtDateTime(o.from, tz)}`,
    duration: `${o.duration_s == null ? "–" : durText(o.duration_s)}${o.ongoing ? " (ongoing)" : ""}`,
    type: OUTAGE_TYPES[o.type] ?? { cls: "neu", text: o.type },
    cause: o.cause ?? "not documented",
    documented: o.source === "annotation",
  }));
}
