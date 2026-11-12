// What the shell and the Overview say about a sensor, an event's severity and
// status, a security level - pure, tested in status.test.js.

import { fmtDateTime, fmtDuration, toDate } from "../format.js";

// A sensor's state against now. A sensor that uploads live (s.contact, schema
// 14) has the api's status (docs/live-upload.md 8.2): online, catching_up,
// clock_unverified, not_capturing, unreachable, discarding (discard mode on,
// schema 20) - "last seen" counts from the last contact. One seen only through snapshot imports has data up to its
// last import (SPEC Q7): it reads "data until <time>" once that poll is older
// than the offline threshold, not "offline"; it is offline when its data
// itself ends in an outage.
export function sensorState(s, offlineMin, now, tz) {
  if (s.contact) return liveState(s, now);
  const lastPoll = toDate(s.last_poll);
  if (!lastPoll) return { tone: "neu", dot: "d-neu", short: "no data", badge: "No data" };
  const lastCap = toDate(s.last_capture_at);
  const nowMs = toDate(now).getTime();
  const limit = (offlineMin ?? 15) * 60e3;
  if (!lastCap || lastPoll.getTime() - lastCap.getTime() > limit) {
    const since = lastCap ? fmtDuration((nowMs - lastCap.getTime()) / 1000) : null;
    return {
      tone: "bad", dot: "d-bad", offline: true,
      short: since ? `offline ${since}` : "offline", badge: since ? `Offline ${since}` : "Offline",
      since: s.last_capture_at,
    };
  }
  if (nowMs - lastPoll.getTime() > limit) {
    const until = fmtDateTime(s.last_poll, tz);
    return { tone: "neu", dot: "d-neu", short: `data until ${until}`, badge: `Data until ${until}` };
  }
  return { tone: "ok", dot: "d-ok", short: "online", badge: "Online" };
}

function liveState(s, now) {
  const nowMs = toDate(now).getTime();
  const ago = (t) => (toDate(t) ? fmtDuration(Math.max(0, nowMs - toDate(t).getTime()) / 1000) : null);
  const c = s.contact;
  switch (s.status) {
    case "unreachable": {
      const since = ago(c.last_contact_at);
      return { tone: "bad", dot: "d-bad", offline: true, live: true, since: c.last_contact_at,
               short: since ? `unreachable ${since}` : "unreachable",
               badge: since ? `Unreachable · last seen ${since} ago` : "Unreachable" };
    }
    case "not_capturing": {
      const since = ago(s.last_capture_at);
      return { tone: "bad", dot: "d-bad", offline: true, live: true, since: s.last_capture_at,
               short: since ? `not capturing ${since}` : "not capturing",
               badge: since ? `Not capturing ${since}` : "Not capturing" };
    }
    case "discarding":
      return { tone: "warn", dot: "d-warn", live: true, discarding: true, short: "discard mode",
               badge: "Discard mode · not stored" };
    case "clock_unverified":
      return { tone: "warn", dot: "d-warn", live: true, short: "clock unverified", badge: "Clock unverified" };
    case "catching_up": {
      const n = c.backlog_polls ?? 0;
      return { tone: "warn", dot: "d-warn", live: true, short: `catching up · ${n} polls`,
               badge: `Catching up · ${n} polls` };
    }
    default:
      return { tone: "ok", dot: "d-ok", live: true, short: "online", badge: "Online" };
  }
}

// "3 h 15 min left", "45 min left", "ends now" (under a minute)
export function leftText(until, now) {
  const s = Math.floor((toDate(until).getTime() - toDate(now).getTime()) / 1000);
  if (!(s >= 60)) return "ends now";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${[h ? `${h} h` : null, m ? `${m} min` : null].filter(Boolean).join(" ")} left`;
}

// The banner of discard mode (schema 20, docs/discard-mode.md) on every page
// of the shell: one per sensor whose window is on (/api/v2/sensors: discard)
// - what it means for the data, its end, what it discarded, the note.
export function discardBanners(sensors, now, tz) {
  return (sensors ?? []).filter((s) => s.discard).map((s) => {
    const d = s.discard;
    const left = leftText(d.until, now);
    return {
      sensor: s.name,
      href: `/settings/sensors?sensor=${encodeURIComponent(s.name)}`,
      head: `Discard mode on for ${s.name} until ${fmtDateTime(d.until, tz)} (${left})`,
      text: `Uploads are accepted but not stored - ${d.polls ?? 0} polls discarded so far. `
        + "The window is excluded from loss, outages and coverage."
        + (d.note ? ` Note: ${d.note}` : ""),
    };
  });
}

export const SEVERITY = {
  critical: { label: "Critical", cls: "bad", rank: 0 },
  high: { label: "High", cls: "bad", rank: 1 },
  medium: { label: "Medium", cls: "warn", rank: 2 },
  low: { label: "Low", cls: "neu", rank: 3 },
  info: { label: "Info", cls: "neu", rank: 4 },
};

export const STATUS = {
  open: { label: "Open", cls: "neu" },
  confirmed: { label: "Confirmed", cls: "ok" },
  benign: { label: "Benign", cls: "neu" },
  false_positive: { label: "False positive", cls: "info" },
};

// Needs attention: the item's background by severity (Main board)
export const ATTENTION_BG = { critical: "#FBF7F6", high: "#FBF7F6", medium: "#FDF8F1", low: "#F7F8F6", info: "#F7F8F6" };

// "1 high · 1 medium · 1 info": the non-zero severities, most severe first
export function severitySummary(bySeverity) {
  return Object.keys(SEVERITY)
    .filter((k) => bySeverity?.[k])
    .map((k) => `${bySeverity[k]} ${k}`)
    .join(" · ");
}

export const LEVELS = {
  open: { color: "var(--lvl-open)", label: "Open" },
  legacy: { color: "var(--lvl-legacy)", label: "Legacy (WEP, WPA1/TKIP)" },
  psk_no_mfp: { color: "var(--lvl-psk)", label: "WPA2-PSK without MFP" },
  strong: { color: "var(--lvl-strong)", label: "Strong (MFP or Enterprise)" },
  unknown: { color: "var(--lvl-unknown)", label: "Unknown" },
};
