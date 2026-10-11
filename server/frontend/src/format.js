// Formatting of the dashboard (SPEC section 2): dates "10 Oct 17:52",
// weekday "Sat 10 Oct", 24-hour clock, all in the account's time zone (the
// api sends UTC; the zone comes with every response as account.tz); thousands
// with a comma, decimal point, a real minus sign "−" for dBm. Pure functions,
// tested in format.test.js.

const formatters = new Map();

function formatter(tz) {
  let f = formatters.get(tz);
  if (!f) {
    // en-US for the short names ("Sep", not en-GB's "Sept"); the order is ours
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, weekday: "short", day: "numeric", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    });
    formatters.set(tz, f);
  }
  return f;
}

export function toDate(v) {
  if (v == null || v === "") return null;
  const d = v instanceof Date ? v : new Date(typeof v === "number" ? v : String(v));
  return Number.isNaN(d.getTime()) ? null : d;
}

// {weekday, day, month, year, hour, minute} of an instant in a time zone
export function partsOf(v, tz) {
  const d = toDate(v);
  if (!d) return null;
  const out = {};
  for (const p of formatter(tz).formatToParts(d)) out[p.type] = p.value;
  return {
    weekday: out.weekday, day: Number(out.day), month: out.month, year: Number(out.year),
    hour: out.hour === "24" ? "00" : out.hour, minute: out.minute,
  };
}

export function fmtTime(v, tz) {
  const p = partsOf(v, tz);
  return p ? `${p.hour}:${p.minute}` : "–";
}

export function fmtDate(v, tz) {
  const p = partsOf(v, tz);
  return p ? `${p.day} ${p.month}` : "–";
}

export function fmtDateTime(v, tz) {
  const p = partsOf(v, tz);
  return p ? `${p.day} ${p.month} ${p.hour}:${p.minute}` : "–";
}

export function fmtDay(v, tz) {
  const p = partsOf(v, tz);
  return p ? `${p.weekday} ${p.day} ${p.month}` : "–";
}

// "45 s", "6 min", "3 h", "5 d" - the largest whole unit
export function fmtDuration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "–";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

// "now", "12 s ago", "6 min ago", "3 h ago", "5 d ago"
export function fmtAgo(v, now = Date.now()) {
  const d = toDate(v);
  if (!d) return "–";
  const s = (toDate(now).getTime() - d.getTime()) / 1000;
  if (s < 5) return "now";
  return `${fmtDuration(s)} ago`;
}

export function fmtInt(n) {
  if (n == null || Number.isNaN(n)) return "–";
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// share 0..1 -> "12 %"
export function fmtPct(share, digits = 0) {
  if (share == null || Number.isNaN(share)) return "–";
  return `${(share * 100).toFixed(digits)} %`;
}

export function fmtNum(v, digits = 1) {
  if (v == null || Number.isNaN(v)) return "–";
  return v.toFixed(digits).replace(/^-/, "−");
}

// dBm with a real minus sign: −63
export function fmtDbm(v) {
  if (v == null || Number.isNaN(v)) return "–";
  return String(Math.round(v)).replace(/^-/, "−");
}

export function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}
