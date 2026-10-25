// The texts of the Events page (boards/Events.dc.html) from the api's rows -
// pure, tested in events.test.js. A feed row is one event or a group
// (item.group: repeated info / low events of one network, type and status
// on one local day; display only). The expanded row: "Why it fired" from the
// event's evidence (one reading per kind: detector, Kismet alert episode,
// security change, sensor outage) and "Who saw it" from who_saw_it.

import { SEVERITY, STATUS } from "../../app/status.js";
import { fmtDate, fmtDateTime, fmtDuration, fmtInt, fmtTime, partsOf } from "../../format.js";

export const STATUSES = ["open", "confirmed", "benign", "false_positive"];   // schema 12: three outcomes
export const KINDS = [["detection", "Detection"], ["kismet", "Kismet alert"], ["config", "Configuration change"],
                      ["sensor", "Sensor"]];
// the "Medium and above" chip: the api's lowest severity
export const SEVERITY_MIN = "medium";

// the status filter of the URL: none = the work queue (Open, decided
// 2026-10-12), "all" = every status, else the listed ones
export function parseStatus(raw) {
  if (raw === "all") return [];
  const list = (raw ?? "").split(",").filter((s) => STATUSES.includes(s));
  return list.length ? list : ["open"];
}
export function statusParam(list) {
  if (!list.length) return "all";
  return list.length === 1 && list[0] === "open" ? null : list.join(",");
}

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

// a group's line under its summary: "3 episodes on 11 Oct, 02:17 – 17:31"
const NOUNS = { kismet: "episodes", config: "changes", detection: "detections" };
export function groupText(e, tz) {
  const g = e.group;
  const end = g.ts_last ?? e.ts;
  const same = partsOf(g.ts_first, tz).day === partsOf(end, tz).day;
  return `${fmtInt(g.n)} ${NOUNS[e.kind] ?? "events"} on ${fmtDate(g.ts_first, tz)}, ${fmtTime(g.ts_first, tz)} – `
    + `${same ? fmtTime(end, tz) : fmtDateTime(end, tz)}`;
}

// the BSSID cell: the event's, a group's one BSSID, or "8 BSSIDs" (a network's APs)
export function bssidCell(e) {
  if (e.group && e.group.bssids > 1) {
    return { text: `${fmtInt(e.group.bssids)} BSSIDs`, mono: false,
             title: [...new Set(e.group.members.map((m) => m.bssid).filter(Boolean))].join(", ") };
  }
  return { text: e.ap?.bssid ?? "–", mono: true, title: undefined };
}

// the feed row that holds an event: its own or the group it is in (null: not on the page)
export function rowOf(items, id) {
  if (id == null) return null;
  return items.find((e) => e.id === id || e.group?.members.some((m) => m.id === id)) ?? null;
}

// "271 open · 271 in total · newest first"
export function headText(counts) {
  return `${fmtInt(counts.open)} open · ${fmtInt(counts.total)} in total · newest first`;
}

export function statusChips(counts) {
  return STATUSES.map((s) => ({ key: s, label: `${STATUS[s].label} ${fmtInt(counts?.[s] ?? 0)}` }));
}

// the events and feed rows of the selected statuses (none: all)
export function selected(counts, status) {
  return status.length ? status.reduce((a, s) => a + (counts?.[s] ?? 0), 0) : (counts?.total ?? 0);
}

// "1–50 of 271 · only the newest are loaded, older ones on request"; with
// groups "1–50 of 159 rows · 271 events, repeated low ones grouped per network and day"
export function pagerText(page, limit, shown, rows, events) {
  if (!shown) return "no event matches the filters";
  const first = (page - 1) * limit + 1;
  const range = `${fmtInt(first)}–${fmtInt(first + shown - 1)}`;
  if (rows === events) return `${range} of ${fmtInt(events)} · only the newest are loaded, older ones on request`;
  return `${range} of ${fmtInt(rows)} rows · ${fmtInt(events)} events, repeated low ones grouped per network and day`;
}

// The three outcomes of a label and their reasons (schema 12; RQ2 splits by
// them) - the same lists as api/app/v2/writes.py REASONS.
export const OUTCOMES = [
  { key: "confirmed", action: "Confirm", label: "Confirmed", hint: "a real security event",
    reasons: [["security_relevant", "Security-relevant"], ["staged_test", "Staged test (own attack run)"]] },
  { key: "benign", action: "Benign", label: "Benign", hint: "the condition was real, but no threat",
    reasons: [["expected_behaviour", "Expected behaviour"], ["accepted_risk", "Accepted risk"]] },
  { key: "false_positive", action: "False positive", label: "False positive", hint: "no real condition, or misattributed",
    reasons: [["rule_defect", "Rule defect or misattribution"], ["artefact", "Sensor or environment artefact"]] },
];
const REASON_TEXT = Object.fromEntries(OUTCOMES.flatMap((o) => o.reasons));

// The cause of a benign or false-positive label (schema 17, RQ2): the same
// list as api/app/v2/writes.py CAUSES (tests/test_schema_files.py compares
// them). "No known cause" is an answer too; "Other" needs a note.
export const CAUSES = [
  ["rssi_floor", "RSSI floor censoring (-106/-120 dBm read as a level)"],
  ["channel_hopping", "Channel hopping / reduced channel coverage"],
  ["kismet_rule", "Kismet rule defect or no security event (e.g. NOCLIENTMFP)"],
  ["allow_list_incomplete", "Incomplete allow-list"],
  ["pipeline_bug", "Bug in this project's pipeline"],
  ["sensor_degraded", "Capture down or no frames"],
  ["ap_behaviour", "Normal AP behaviour"],
  ["client_behaviour", "Normal client behaviour"],
  ["staged_test", "Own staged test (spill-over)"],
  ["unknown", "No known cause"],
  ["other", "Other (name it in the note)"],
];
export const WITH_CAUSE = ["benign", "false_positive"];
const CAUSE_TEXT = Object.fromEntries(CAUSES);

export function causeText(cause) {
  return CAUSE_TEXT[cause] ?? cause ?? "";
}

export function reasonText(reason) {
  return REASON_TEXT[reason] ?? reason ?? "";
}

// "Benign · Expected behaviour · open guest network"
export function labelText(e) {
  if (!e.status || e.status === "open") return null;
  const o = OUTCOMES.find((x) => x.key === e.status);
  return [o?.label ?? e.status, reasonText(e.status_reason), e.status_cause ? `cause: ${causeText(e.status_cause)}` : null,
          e.status_note].filter(Boolean).join(" · ");
}

// "13 Oct 14:05 · Open → Benign · Expected behaviour · open guest network": a change of a
// label (audit_log keeps the old value next to the new one); the note is the new one's
export function historyLine(h, tz) {
  const name = (x) => (x.status === "open" ? "Open" : OUTCOMES.find((o) => o.key === x.status)?.label ?? x.status);
  const side = (x, note) => (x ? [name(x), reasonText(x.reason), x.cause ? `cause: ${causeText(x.cause)}` : null,
                                   note ? x.note : null].filter(Boolean).join(" · ") : "–");
  return `${fmtDateTime(h.at, tz)} · ${side(h.from, false)} → ${side(h.to, true)}`;
}

// the action of a row: Label (any of the three outcomes, on every kind -
// decided 2026-10-13; chosen in the form) or, once labelled, Relabel; one
// button keeps the table within the page
export function actionsFor(e) {
  return e.status === "open" ? ["label"] : ["relabel"];
}
export function actionText(key, group = false) {
  if (key === "relabel") return group ? "Relabel all" : "Relabel";
  if (key === "label") return group ? "Label all" : "Label";
  const o = OUTCOMES.find((x) => x.key === key);
  return group ? `${o.action} all` : o.action;
}

// the label body: {status, reason, cause, note}, or {error}; benign and
// false positive need a cause, "other" a note (writes.py parse_status)
export function labelBody(outcome, reason, cause, note) {
  const n = note?.trim() || null;
  if (outcome === "open") return { status: "open", reason: null, cause: null, note: n };
  const o = OUTCOMES.find((x) => x.key === outcome);
  if (!o) return { error: "pick an outcome" };
  if (!o.reasons.some(([k]) => k === reason)) return { error: `pick why it is ${o.label.toLowerCase()}` };
  if (!WITH_CAUSE.includes(outcome)) return { status: outcome, reason, cause: null, note: n };
  if (!CAUSES.some(([k]) => k === cause)) return { error: "pick the cause (or No known cause)" };
  if (cause === "other" && !n) return { error: "name the cause in the note" };
  return { status: outcome, reason, cause, note: n };
}

// the events a selection or a group row labels
export function idsOf(rows) {
  return [...new Set(rows.flatMap((e) => (e.group ? e.group.members.map((m) => m.id) : [e.id])))].sort((a, b) => a - b);
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
