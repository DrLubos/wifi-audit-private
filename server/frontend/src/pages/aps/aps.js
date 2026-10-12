// The texts of the Access points list and the AP page (boards/Aps.dc.html,
// boards/ApDetail.dc.html) from the api's rows - pure, tested in aps.test.js.

import { SEVERITY, STATUS } from "../../app/status.js";
import { fmtDate, fmtDateTime, fmtDay, fmtInt, fmtTime, partsOf, toDate } from "../../format.js";
import { levelBadge, shortVendor } from "../networks/networks.js";

export const CLASSES = ["managed", "friendly", "unknown", "suspicious", "ignored"];
const CLASS_BADGE = {
  managed: { cls: "info", text: "Managed" }, friendly: { cls: "ok", text: "Friendly" },
  unknown: { cls: "neu", text: "Unknown" }, suspicious: { cls: "bad", text: "Suspicious" },
  ignored: { cls: "mute", text: "Ignored" },
};

export function classBadge(c) {
  return CLASS_BADGE[c] ?? { cls: "neu", text: c ?? "–" };
}

export function statusBadge(active) {
  return active ? { cls: "ok", text: "Active" } : { cls: "neu", text: "Inactive" };
}

// the list's name: the SSID (or the name seen for a hidden AP), else "hidden network" in italics
export function apName(a) {
  if (a.network) return { text: a.network, hidden: false };
  return a.hidden ? { text: "hidden network", hidden: true } : { text: a.ssid || "–", hidden: false };
}

// "Ruckus", "– (randomized)" for a locally administered BSSID without a vendor
export function vendorText(a) {
  return shortVendor(a.manuf) ?? (a.random_bssid ? "– (randomized)" : "–");
}

// the configuration label in its level's color; "allowed" when the policy
// accepts an open network, "policy not met" (with the reason) when it fails
export function securityCell(a) {
  return {
    cls: levelBadge(a.security),
    text: a.label ?? "Unknown",
    note: a.policy_met === true && a.security === "open" ? "allowed"
      : a.policy_met === false ? "policy not met" : null,
    title: a.policy_met === false ? a.policy_reason : undefined,
  };
}

export function channelText(a) {
  return a.channel ? `${a.channel}${a.band ? ` · ${a.band} GHz` : ""}` : "–";
}

export function utilText(v) {
  return v == null ? "–" : `${Math.round(v)} %`;
}

// "now", "6 min ago", "2 h ago", "yesterday", "8 Oct", counted from ref: the
// latest capture of the sensors (decided 2026-10-12 - between imports the
// data ends there, not at the wall clock)
export function lastSeenText(t, ref, tz) {
  const d = toDate(t);
  if (!d) return "–";
  const s = Math.max(0, (toDate(ref).getTime() - d.getTime()) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400 && partsOf(d, tz).day === partsOf(ref, tz).day) return `${Math.floor(s / 3600)} h ago`;
  if (s < 2 * 86400 && partsOf(d.getTime() + 86400e3, tz).day === partsOf(ref, tz).day) return "yesterday";
  return fmtDate(d, tz);
}

// the tooltip: the absolute time and what the relative one counts from
export function lastSeenTitle(t, ref, tz) {
  return t ? `${fmtDateTime(t, tz)} · counted from the sensor's latest capture, ${fmtDateTime(ref, tz)}` : undefined;
}

// the reference of "last seen": the newest capture of the given sensors
// (names; all of them without), else the wall clock
export function latestCapture(sensors, names, now) {
  const caps = (sensors ?? []).filter((s) => !names || names.includes(s.name)).map((s) => toDate(s.last_capture_at))
    .filter(Boolean).map((d) => d.getTime());
  return caps.length ? new Date(Math.max(...caps)).toISOString() : toDate(now).toISOString();
}

// SPEC section 4: an AP's clients = the stations its BSS Load element reports;
// without one, Kismet's num_associated_clients - every client seen associated
// since Kismet began tracking the AP (Kismet 2025-09-R1 never prunes the map),
// so not a current count: shown muted, said so in the tooltip
const SENSOR_COUNT = "the sensor's count (Kismet): every client seen associated since it began tracking the AP, "
  + "not a current number - this AP sends no BSS Load element";
export function clientsCell(a) {
  if (a.clients == null) return { text: "–", muted: false, title: undefined };
  return a.clients_source === "sensor"
    ? { text: fmtInt(a.clients), muted: true, title: SENSOR_COUNT }
    : { text: fmtInt(a.clients), muted: false, title: "stations the AP reports in its BSS Load element" };
}

// the AP page's load numbers, the AP's own report first (decided 2026-10-12)
export function loadNumbers(load) {
  return [
    { label: "Stations reported by the AP", value: load.qbss_stations == null ? "–" : fmtInt(load.qbss_stations),
      title: "the station count of the AP's BSS Load element" },
    { label: "Channel utilization", value: load.util == null ? "–" : `${Math.round(load.util)} %`,
      title: "the airtime the AP reports busy in its BSS Load element" },
    { label: "Clients seen by the sensor (cumulative)",
      value: load.kismet_clients == null ? "–" : fmtInt(load.kismet_clients),
      title: "Kismet's num_associated_clients: every client seen associated since Kismet began tracking this AP "
        + "(the list is never pruned) - not how many are connected now", secondary: true },
  ];
}

// "Seen by sensors (RSSI)": strongest first (the api's order), "pi-fri −63"
export function seenByChips(seenBy) {
  return (seenBy ?? []).map((s) => ({
    name: s.sensor,
    text: s.rssi_median == null ? s.sensor : `${s.sensor} ${String(Math.round(s.rssi_median)).replace("-", "−")}`,
  }));
}

// "1–50 of 603 (100 hotspots hidden)"
export function pagerText(page, pageSize, total, hiddenHotspots, shown) {
  const first = (page - 1) * pageSize;
  const range = total ? `${fmtInt(first + 1)}–${fmtInt(first + shown)} of ${fmtInt(total)}` : "none";
  return hiddenHotspots ? `${range} (${fmtInt(hiddenHotspots)} hotspots hidden)` : range;
}

// the chips: classification (one at a time, "All" clears it) and the toggles
export function chips(counts) {
  const c = counts ?? {};
  return {
    classes: [{ key: null, label: `All ${fmtInt(c.all ?? 0)}` },
              ...CLASSES.map((k) => ({ key: k, label: `${CLASS_BADGE[k].text} ${fmtInt(c[k] ?? 0)}` }))],
    toggles: [{ key: "band24", label: "2.4 GHz", n: c.band_2_4 }, { key: "band5", label: "5 GHz", n: c.band_5 },
              { key: "open", label: "Open", n: c.open }, { key: "new", label: "New only", n: c.new },
              { key: "hotspots", label: "Hide hotspots", n: c.hotspots }],
  };
}

// a group header of the list: "IK-WIFI · 28 APs", "hidden network …", "pi-fri · 120 APs"
export function groupLabel(group, key, n) {
  const name = group === "ssid" && key?.startsWith("hidden:") ? `hidden network ${key.slice(7)}` : key || "no sensor";
  return `${name} · ${fmtInt(n)} ${n === 1 ? "AP" : "APs"}`;
}

// a Kismet device key of the previous pages ("4202770D00000000_F62CE5D4C3B2"):
// its second part is the MAC byte-reversed (api privacy.key_mac)
export function bssidOfKey(key) {
  const m = /^[0-9A-Fa-f]{16}_([0-9A-Fa-f]{12})$/.exec(key ?? "");
  if (!m) return null;
  const h = m[1].toUpperCase();
  const out = [];
  for (let i = 10; i >= 0; i -= 2) out.push(h.slice(i, i + 2));
  return out.join(":");
}

export function isBssid(s) {
  return /^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$/.test(s ?? "");
}

// --- the AP page ---------------------------------------------------------------------------

// HT20 -> 20 MHz, HT40+ -> 40 MHz, HT80 / VHT80 -> 80 MHz, VHT160 -> 160 MHz
export function widthText(ht) {
  const m = /(\d+)/.exec(ht ?? "");
  return m ? `${m[1]} MHz` : null;
}

// "channel 48 (5 GHz, 80 MHz)"
export function channelLine(id) {
  if (!id.channel) return "channel unknown";
  const parts = [id.band ? `${id.band} GHz` : null, widthText(id.ht_mode)].filter(Boolean);
  return `channel ${id.channel}${parts.length ? ` (${parts.join(", ")})` : ""}`;
}

// "48 · 5 GHz · 80 MHz (HT80)"
export function channelDetail(id) {
  if (!id.channel) return "–";
  const w = widthText(id.ht_mode);
  return [id.channel, id.band ? `${id.band} GHz` : null, w ? `${w} (${id.ht_mode})` : null].filter(Boolean).join(" · ");
}

// "WPA2-Enterprise (802.1X) · CCMP"
export function encryptionText(sec) {
  if (!sec.label || sec.level === "unknown") return "unknown (no beacon record)";
  const akm = sec.detail?.akm?.length ? ` (${sec.detail.akm.join(", ")})` : "";
  const ciphers = [...new Set([...(sec.detail?.pairwise ?? [])])];
  return `${sec.label}${akm}${ciphers.length ? ` · ${ciphers.join(", ")}` : ""}`;
}

const MFP = { required: "required", capable: "capable, not required", no: "not supported" };
export function mfpText(mfp) {
  return MFP[mfp] ?? "unknown";
}

const RATING = { open: ["bad", "Open"], legacy: ["bad", "Legacy"], psk_no_mfp: ["warn", "WPA2-PSK without MFP"],
                 strong: ["ok", "Strong"], unknown: ["neu", "Unknown"] };
export function ratingBadge(level) {
  const [cls, text] = RATING[level] ?? RATING.unknown;
  return { cls, text };
}

// the network policy line: null without a managed network
export function policyBadge(policy, level) {
  if (!policy) return null;
  if (level === "unknown") return { cls: "neu", text: "Not assessed", reason: "security unknown (no beacon record)" };
  return policy.met ? { cls: "ok", text: "Met", reason: null } : { cls: "warn", text: "Not met", reason: policy.reason };
}

const ADVICE = {
  "MFP not required": "Require MFP, otherwise clients stay exposed to forged deauthentication.",
  "not Enterprise": "Switch to 802.1X (Enterprise), as the network policy requires.",
  "weaker than WPA2-PSK": "Turn off WEP, WPA1 and TKIP; use WPA2-PSK (CCMP) or better.",
  "not WPA3": "Enable WPA3 (SAE), or 802.1X with required MFP, as the network policy requires.",
};
export function recommendation(policy, level) {
  if (!policy || policy.met || level === "unknown") return null;
  return ADVICE[policy.reason] ?? null;
}

// "measured by pi-fri at its latest capture" - the AP's own BSS Load element,
// and what the sensor's count is (counted from ref, the latest capture)
export function loadNote(load, ref, tz) {
  if (!load.at) return "No load measured yet.";
  const s = Math.max(0, (toDate(ref).getTime() - toDate(load.at).getTime()) / 1000);
  const when = s < 60 ? "at its latest capture" : `${lastSeenText(load.at, ref, tz)}, ${fmtDateTime(load.at, tz)}`;
  const by = load.sensor ? `measured by ${load.sensor} ${when}` : `measured ${when}`;
  const own = load.qbss_stations != null || load.util != null
    ? `Stations and utilization come from the AP's own BSS Load element, ${by}.`
    : `This AP sends no BSS Load element (${by}).`;
  return `${own} The sensor's count adds up every client seen associated since it began tracking the AP.`;
}

const FIELD = { adv_channel: "Channel", ht_mode: "Channel width", crypt: "Encryption", crypt_bits: "Encryption bits",
                mfp_sup: "MFP capable", mfp_req: "MFP required", ssid: "SSID", cloaked: "Hidden SSID",
                beacon_rate: "Beacon rate", country: "Country" };
const value = (v) => (v === "true" || v === true ? "yes" : v === "false" || v === false ? "no"
  : v == null || v === "" ? "–" : String(v));

// a configuration change: {what: "Channel", from: "64", to: "48"}
export function changeParts(c) {
  return { what: FIELD[c.field] ?? c.field, from: value(c.old), to: value(c.new) };
}

// "changes channel ~12× per day (dynamic channel selection)"
export function changesPerDayText(n) {
  if (n == null || n < 1) return null;
  return `changes channel ~${Math.round(n)}× per day${n >= 4 ? " (dynamic channel selection)" : ""}`;
}

// the history list: the changes newest first, the first capture per sensor after its time
export function historyRows(history, firstCapture, tz) {
  const rows = (history?.rows ?? []).map((h) => ({
    ts: h.ts, when: fmtDateTime(h.ts, tz), sensor: h.sensor, parts: h.changes.map(changeParts),
  }));
  for (const f of firstCapture ?? []) {
    if (f.ts) rows.push({ ts: f.ts, when: fmtDateTime(f.ts, tz), sensor: f.sensor, first: true });
  }
  return rows.sort((a, b) => toDate(b.ts) - toDate(a.ts));
}

export function eventBadges(e) {
  const sev = SEVERITY[e.severity] ?? SEVERITY.info;
  const st = e.status && e.status !== "open" ? STATUS[e.status] : null;
  return { severity: { cls: sev.cls, text: sev.label }, status: st ? { cls: st.cls, text: st.label } : null };
}

// --- RSSI chart ----------------------------------------------------------------------------

const dbm = (v) => `${String(Math.round(v)).replace("-", "−")} dBm`;

// the y range in dBm: the readings and the baseline bands (median ± 2 robust σ),
// out to 5 dB steps, at least 30 dB; ticks every quarter
export function rssiScale(series, baselines) {
  const vals = [];
  for (const s of Object.values(series ?? {})) for (const v of s) if (v != null) vals.push(v);
  for (const b of Object.values(baselines ?? {})) {
    if (b.median != null) vals.push(b.median + 2 * (b.robust_sd ?? 0), b.median - 2 * (b.robust_sd ?? 0));
  }
  if (!vals.length) return { top: -50, bottom: -95, ticks: [-50, -65, -80, -95] };
  let top = Math.ceil(Math.max(...vals) / 5) * 5;
  let bottom = Math.floor(Math.min(...vals) / 5) * 5;
  if (top - bottom < 30) {
    const mid = (top + bottom) / 2;
    top = Math.ceil((mid + 15) / 5) * 5;
    bottom = top - 30;
  }
  const step = (top - bottom) / 3;
  return { top, bottom, ticks: [0, 1, 2, 3].map((k) => Math.round(top - k * step)) };
}

// polylines (x 0..1000 at the middle of each hour, y 0..h) split at the gaps
export function rssiLines(values, top, bottom, h = 220) {
  const n = values.length;
  const lines = [];
  let cur = [];
  values.forEach((v, i) => {
    if (v == null) {
      if (cur.length) lines.push(cur);
      cur = [];
      return;
    }
    cur.push(`${(((i + 0.5) / n) * 1000).toFixed(1)},${(((top - v) / (top - bottom)) * h).toFixed(1)}`);
  });
  if (cur.length) lines.push(cur);
  return lines.map((pts) => (pts.length === 1 ? [pts[0], pts[0].replace(/^([\d.]+)/, (x) => String(+x + 3))] : pts).join(" "));
}

// the baseline band (median ± 2 robust σ) as y and height in the 220 px plot
export function bandRect(b, top, bottom, h = 220) {
  const hi = b.median + 2 * (b.robust_sd ?? 0);
  const lo = b.median - 2 * (b.robust_sd ?? 0);
  const y = ((top - hi) / (top - bottom)) * h;
  return { y: Math.max(0, y), height: Math.max(1, ((hi - lo) / (top - bottom)) * h) };
}

// the band's horizontal span in the 0..1000 viewBox: from the start of the
// baseline's window (inside one capture-hardware period, schema 11) to the
// chart's end; the whole width without a start or when it lies before the chart
export function bandSpan(b, t, bucketS = 3600) {
  const from = b.from ? Date.parse(b.from) / 1000 : null;
  if (from == null || !t.length) return { x: 0, width: 1000 };
  const t0 = t[0];
  const t1 = t[t.length - 1] + bucketS;
  const x = Math.round(Math.min(1, Math.max(0, (from - t0) / (t1 - t0))) * 1000);
  return { x, width: 1000 - x };
}

// the tooltip of hour i: the reading per sensor, its baseline, outside the band
export function rssiTip(i, d, colors, tz) {
  const t = d.t[i] * 1000;
  const head = `${fmtDay(t, tz)}, ${fmtTime(t, tz)}–${fmtTime(t + 3600e3, tz)}`;
  const rows = [];
  const notes = [];
  for (const [name, vals] of Object.entries(d.series)) {
    const v = vals[i];
    const floors = d.n_floor?.[name]?.[i] ?? 0;
    rows.push({ c: colors[name], v: v == null ? "–" : dbm(v),
                l: v == null ? (floors ? `${name}: floor readings only` : `${name}: no reading`) : name });
    const b = d.baseline?.[name];
    if (b && v != null && Math.abs(v - b.median) > 2 * (b.robust_sd ?? 0)) {
      notes.push(`${name}: outside its baseline band (${dbm(b.median + 2 * b.robust_sd)} to ${dbm(b.median - 2 * b.robust_sd)})`);
    }
  }
  return { head, rows, notes };
}

// x labels at the local midnights of the hourly grid
export function rssiTicks(t, tz) {
  const n = t.length;
  const out = [];
  t.forEach((s, i) => {
    const p = partsOf(s * 1000, tz);
    if (p && p.hour === "00") out.push({ t: fmtDay(s * 1000, tz), left: `${((i / n) * 100).toFixed(2)}%` });
  });
  return out.filter((x, i) => i === 0 || parseFloat(x.left) <= 92);
}

// "Median 12 %, peak 30 % (Sun 11 Oct, 18:00–24:00)."
export function utilSummary(avg, peak, labels) {
  const a = avg.filter((v) => v != null).sort((x, y) => x - y);
  if (!a.length) return null;
  let best = -1;
  peak.forEach((v, i) => { if (v != null && (best < 0 || v > peak[best])) best = i; });
  return `Median ${Math.round(a[Math.floor(a.length / 2)])} %, peak ${Math.round(peak[best])} % (${labels[best]}).`;
}

// active now (Q8, as the list): seen within 5 min of the last collecting poll
// of a sensor that saw it
export function isActive(lastSeen, apSensors, accountSensors) {
  const names = new Set((apSensors ?? []).map((s) => s.name));
  const caps = (accountSensors ?? []).filter((s) => names.has(s.name) && s.last_capture_at)
    .map((s) => toDate(s.last_capture_at).getTime());
  const seen = toDate(lastSeen);
  return Boolean(seen && caps.length && seen.getTime() >= Math.max(...caps) - 5 * 60e3);
}
