// Display formatting. Numbers stay ASCII so they sort and copy cleanly.

const intFormat = new Intl.NumberFormat("en-US");

export function fmtInt(n) {
  return n == null ? "-" : intFormat.format(Math.round(n));
}

export function fmtNum(n, digits = 1) {
  return n == null ? "-" : Number(n).toFixed(digits);
}

export function fmtDbm(n) {
  if (n == null) return "-";
  return `${fmtNum(n, Number.isInteger(n) ? 0 : 1)} dBm`;
}

export function fmtDb(n) {
  return n == null ? "-" : `${fmtNum(n, 1)} dB`;
}

export function fmtPct(n) {
  return n == null ? "-" : `${fmtNum(n, 1)} %`;
}

export function fmtDuration(seconds) {
  if (seconds == null) return "-";
  const s = Math.round(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

const pad = (n) => String(n).padStart(2, "0");

// Local time, ISO-like, minute precision: 2026-09-15 15:53
export function fmtDateTime(value) {
  if (value == null) return "-";
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return "-";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtDate(value) {
  return fmtDateTime(value).slice(0, 10);
}

// Kismet frequencies are kHz.
export function bandOf(freqKhz) {
  if (!freqKhz) return null;
  if (freqKhz < 3_000_000) return "2.4 GHz";
  if (freqKhz < 5_925_000) return "5 GHz";
  return "6 GHz";
}
