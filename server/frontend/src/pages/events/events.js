// The texts of the Events page (boards/Events.dc.html) from the api's rows -
// pure, tested in events.test.js. The expanded row: "Why it fired" from the
// event's evidence (one reading per kind: detector, Kismet alert episode,
// security change, sensor outage) and "Who saw it" from who_saw_it.

import { SEVERITY, STATUS } from "../../app/status.js";
import { fmtDate, fmtDateTime, fmtDuration, fmtInt, fmtTime, partsOf } from "../../format.js";

export const STATUSES = ["open", "confirmed", "false_positive"];
export const KINDS = [["detection", "Detection"], ["kismet", "Kismet alert"], ["config", "Configuration change"],
                      ["sensor", "Sensor"]];

export function severityBadge(s) {
  const v = SEVERITY[s] ?? SEVERITY.info;
  return { cls: v.cls, text: v.label };
}

export function statusBadge(s) {
  const v = STATUS[s] ?? STATUS.open;
  return { cls: v.cls, text: v.label };
}

// "Detection · Deauthentication flood", "Kismet · DEAUTHFLOOD", "Configuration change", "Sensor"
export function typeText(e) {
  if (e.kind === "detection") return `Detection · ${e.type_label}`;
  if (e.kind === "kismet") return `Kismet · ${e.type}`;
  if (e.kind === "config") return "Configuration change";
  return "Sensor";
}

// the AP / network cell: the network, "hidden network" (italics) or "–" (sensor events)
export function apCell(e) {
  if (!e.ap) return { text: "–", hidden: false };
  if (e.ap.network) return { text: e.ap.network, hidden: false };
  return { text: "hidden network", hidden: true };
}

// "10 Oct 17:46"; an episode adds its end in the tooltip
export function timeText(e, tz) {
  return fmtDateTime(e.ts, tz);
}
export function timeTitle(e, tz) {
  if (!e.ts_end || e.ts_end === e.ts) return undefined;
  const same = partsOf(e.ts, tz).day === partsOf(e.ts_end, tz).day;
  return `${fmtDateTime(e.ts, tz)} – ${same ? fmtTime(e.ts_end, tz) : fmtDateTime(e.ts_end, tz)}`;
}

// "271 open · 271 in total · newest first"
export function headText(counts) {
  return `${fmtInt(counts.open)} open · ${fmtInt(counts.total)} in total · newest first`;
}

export function statusChips(counts) {
  return STATUSES.map((s) => ({ key: s, label: `${STATUS[s].label} ${fmtInt(counts?.[s] ?? 0)}` }));
}

// "1–50 of 271 · only the newest are loaded, older ones on request"
export function pagerText(page, limit, shown, counts, filtered) {
  if (!shown) return "no event matches the filters";
  const first = (page - 1) * limit + 1;
  const total = filtered ?? counts.total;
  return `${fmtInt(first)}–${fmtInt(first + shown - 1)} of ${fmtInt(total)} · only the newest are loaded, older ones on request`;
}

// the actions an event offers (locked until the write token, phase 5): an open
// detection or Kismet alert can be confirmed or marked a false positive, an
// open configuration change or sensor event only confirmed
export function actionsFor(e) {
  if (e.status !== "open") return [];
  return e.kind === "detection" || e.kind === "kismet" ? ["Confirm", "False positive"] : ["Confirm"];
}

const pct = (v) => `${Math.round(v * 10) / 10}`;
const dur = (s) => (s < 1 ? `${Math.round(s * 1000)} ms` : s < 60 ? `${Math.round(s)} s` : fmtDuration(s));

// "Why it fired": [{dt, dd}] from the evidence, per kind
export function whyRows(ev, tz) {
  const x = ev.evidence ?? {};
  if (ev.kind === "kismet") {
    const rows = [{ dt: "Kismet alert", dd: x.header ?? ev.type },
                  { dt: "Alerts", dd: `${fmtInt(x.alerts ?? 1)}${x.first && x.last && x.first !== x.last
                    ? `, ${fmtDateTime(x.first, tz)} – ${fmtTime(x.last, tz)}` : ""}` }];
    const ch = (x.channels ?? []).filter((c) => c && c !== "0");
    if (ch.length) rows.push({ dt: "Channel", dd: ch.join(", ") });
    if (x.kismet_severity != null) rows.push({ dt: "Kismet severity", dd: String(x.kismet_severity) });
    return rows;
  }
  if (ev.kind === "config") {
    const f = x.from ?? {};
    const t = x.to ?? {};
    const rows = [];
    if (f.label !== t.label) rows.push({ dt: "Security", dd: `${f.label ?? "unknown"} → ${t.label ?? "unknown"}` });
    if (f.mfp !== t.mfp) rows.push({ dt: "MFP", dd: `${f.mfp ?? "unknown"} → ${t.mfp ?? "unknown"}` });
    if (f.crypt !== t.crypt) rows.push({ dt: "Encryption", dd: `${f.crypt ?? "–"} → ${t.crypt ?? "–"}` });
    if (f.level !== t.level) rows.push({ dt: "Level", dd: `${f.level ?? "unknown"} → ${t.level ?? "unknown"}` });
    return rows.length ? rows : [{ dt: "Change", dd: ev.summary }];
  }
  if (ev.kind === "sensor") {
    const rows = [{ dt: "Kind", dd: x.kind === "outage" ? "no poll collected (outage)" : "capture limited (hop list degraded)" },
                  { dt: "Window", dd: `${fmtDateTime(ev.ts, tz)} – ${ev.ts_end ? fmtDateTime(ev.ts_end, tz) : "still going"}` }];
    if (x.duration_s != null) rows.push({ dt: "Duration", dd: fmtDuration(x.duration_s) });
    if (x.polls != null) rows.push({ dt: "Polls in it", dd: fmtInt(x.polls) });
    rows.push({ dt: "Cause", dd: x.cause ?? "not documented" });
    return rows;
  }
  if (ev.type === "unknown_bssid_managed_ssid") {
    const ouis = (x.allowed_ouis ?? []).join(", ");
    const allowed = [x.allowed_bssids ? `${fmtInt(x.allowed_bssids)} BSSIDs` : null, ouis ? `OUIs ${ouis}` : null]
      .filter(Boolean).join(", ");
    const rows = [
      { dt: "BSSID", dd: `${ev.ap?.bssid ?? "–"}${x.random_bssid ? " (randomized)" : ""}`, mono: true },
      { dt: "Broadcasts SSID", dd: `${x.network ?? ev.ap?.network ?? "–"} (managed network)` },
      { dt: "Allowed at the time", dd: `${allowed || "nothing"} – this one is not among them` },
      { dt: "Security", dd: x.security_label ?? "unknown" },
      { dt: "Channel", dd: x.channel ?? "–" },
    ];
    if (ev.ap?.classification && ev.ap.classification !== "suspicious") {
      rows.push({ dt: "Now", dd: `classified ${ev.ap.classification} - the network's rules have changed since` });
    }
    return rows;
  }
  if (ev.type === "deauth_flood") {
    const b = x.bursts ?? {};
    const a = x.alerts ?? {};
    const rules = [];
    if (x.rules?.bursts) rules.push(`${fmtInt(b.n)} deauth/disassoc bursts within ${dur(x.params?.burst_window_s ?? 600)} (threshold ${b.threshold})`);
    if (x.rules?.alerts) rules.push(`${fmtInt(a.n)} Kismet DEAUTHFLOOD alerts within ${dur(a.span_s ?? 0)}`);
    const rows = [{ dt: "Rule", dd: rules.join("; ") || ev.summary }];
    if (x.observed?.bursts_per_hour != null && x.baseline?.bursts_per_hour != null) {
      rows.push({ dt: "Rate", dd: `${pct(x.observed.bursts_per_hour)} bursts/h, the AP's baseline ${pct(x.baseline.bursts_per_hour)}/h`
        + (x.observed.ratio_to_baseline ? ` (${fmtInt(x.observed.ratio_to_baseline)}×)` : "") });
    }
    if (x.baseline?.from) rows.push({ dt: "Baseline", dd: `${fmtDate(x.baseline.from, tz)} – ${fmtDate(x.baseline.to, tz)}, ${fmtInt(x.baseline.polls)} polls` });
    if (x.duration_s != null) rows.push({ dt: "Duration", dd: dur(x.duration_s) });
    if (x.ap) rows.push({ dt: "MFP required", dd: x.ap.mfp_req ? "yes" : "no - clients can be disconnected by forged frames" });
    return rows;
  }
  // another detector: its scalar evidence as it is
  return Object.entries(x).filter(([, v]) => v == null || typeof v !== "object").slice(0, 6)
    .map(([k, v]) => ({ dt: k.replaceAll("_", " "), dd: v == null ? "–" : String(v) }));
}

// "Who saw it": per sensor the median RSSI and the clients in the event's
// hours, and when the sensor first and last saw the AP at all
export function whoRows(ev, tz) {
  return (ev.who_saw_it ?? []).map((w) => {
    const known = `known since ${fmtDate(w.first_seen, tz)}, last seen ${fmtDateTime(w.last_seen, tz)}`;
    return {
      dt: w.sensor,
      dd: w.seen
        ? [w.rssi_median == null ? "only floor readings then"
            : `${String(Math.round(w.rssi_median)).replace("-", "−")} dBm then`,
           w.clients != null ? `up to ${fmtInt(w.clients)} clients then` : null, known].filter(Boolean).join(" · ")
        : `not seen in the event's hours · ${known}`,
    };
  });
}
