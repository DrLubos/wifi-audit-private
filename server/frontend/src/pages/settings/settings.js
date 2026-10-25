// The Settings tabs (boards/Settings.dc.html, SettingsSensors.dc.html) -
// pure, tested in settings.test.js: the drafts the forms edit and the bodies
// the write endpoints take (api/app/v2/writes.py validates the same rules),
// the channel plans with Kismet's hop-stride rule, the hardware texts and the
// configuration history with its data boundaries.

import { fmtDateTime, fmtInt, partsOf } from "../../format.js";

export const TABS = [
  ["networks", "Networks"], ["classification", "Classification"], ["detectors", "Detectors"], ["sensors", "Sensors"],
];

// --- networks -----------------------------------------------------------------------------------

export const EXPECTED = [
  ["enterprise", "Enterprise (802.1X)"], ["psk_or_better", "WPA2-PSK or better"], ["wpa3", "WPA3"],
  ["open_allowed", "Open allowed"],
];

let fresh = 0;
export function networkDraft(n) {
  return {
    key: n.id != null ? `n${n.id}` : `new${(fresh += 1)}`, id: n.id ?? null, ssid: n.ssid ?? "",
    kind: n.kind ?? "managed", expected_security: n.expected_security ?? null, require_mfp: Boolean(n.require_mfp),
    allowed_bssids: [...(n.allowed_bssids ?? [])], allowed_ouis: [...(n.allowed_ouis ?? [])], note: n.note ?? null,
  };
}

// the PUT /settings/networks body (a friendly network is not checked)
export function networksBody(drafts) {
  return {
    networks: drafts.map((d) => ({
      id: d.id, ssid: d.ssid, kind: d.kind,
      expected_security: d.kind === "managed" ? d.expected_security : null,
      require_mfp: d.kind === "managed" && d.require_mfp,
      allowed_bssids: d.allowed_bssids, allowed_ouis: d.allowed_ouis, note: d.note,
    })),
  };
}

export function sameBody(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// a typed allowed entry: 6 octets = a BSSID, 3 = an OUI (vendor prefix)
export function parseAllowed(text) {
  const hex = (text ?? "").trim().replace(/[:\-.\s]/g, "").toUpperCase();
  if (!/^[0-9A-F]+$/.test(hex) || (hex.length !== 12 && hex.length !== 6)) {
    return { error: "a BSSID (EC:58:EA:55:88:FC) or a vendor prefix (EC:58:EA)" };
  }
  const value = hex.match(/../g).join(":");
  return { kind: hex.length === 12 ? "bssid" : "oui", value };
}

export function addAllowed(d, entry) {
  const list = entry.kind === "bssid" ? "allowed_bssids" : "allowed_ouis";
  if (d[list].includes(entry.value)) return d;
  return { ...d, [list]: [...d[list], entry.value].sort() };
}

export function removeAllowed(d, kind, value) {
  const list = kind === "bssid" ? "allowed_bssids" : "allowed_ouis";
  return { ...d, [list]: d[list].filter((v) => v !== value) };
}

// "EC:58:EA (Ruckus)" - the vendor of an OUI when an AP of the account has it
export function ouiText(o, vendors) {
  return vendors?.[o] ? `${vendors[o]} ${o}` : o;
}

// the UTF-8 length of a string (an SSID is at most 32 bytes)
export function utf8Length(s) {
  let n = 0;
  for (const ch of s) {
    const c = ch.codePointAt(0);
    n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
  }
  return n;
}

// the SSID of a new network: not empty, at most 32 bytes, not a second time
export function ssidProblem(ssid, drafts) {
  if (!ssid.trim()) return "enter an SSID";
  if (utf8Length(ssid) > 32) return "an SSID has at most 32 bytes";
  if (drafts.some((d) => d.ssid === ssid)) return `${ssid} is in the list already`;
  return null;
}

// --- classification and detectors ---------------------------------------------------------------

export function classificationDraft(c) {
  return {
    ignore_random_bssid: c.ignore_random_bssid, suspicious_unallowed: c.suspicious_unallowed,
    managed_by_vendor: c.managed_by_vendor, weaker_on: c.ignore_weaker_dbm != null,
    weaker_dbm: String(c.ignore_weaker_dbm ?? -92), vendors: (c.mobile_vendors ?? []).join(", "),
  };
}

export function classificationBody(d) {
  return {
    ignore_random_bssid: d.ignore_random_bssid, suspicious_unallowed: d.suspicious_unallowed,
    managed_by_vendor: d.managed_by_vendor,
    ignore_weaker_dbm: d.weaker_on ? Number.parseInt(d.weaker_dbm, 10) : null,
    mobile_vendors: d.vendors.split(",").map((v) => v.trim()).filter(Boolean),
  };
}

export function classificationProblem(d) {
  if (!d.weaker_on) return null;
  const v = Number(d.weaker_dbm);
  return Number.isInteger(v) && v >= -120 && v <= -30 ? null : "the threshold is a whole number from −120 to −30 dBm";
}

export const GUARDS = [[0, "off"], [600, "10 min"], [1800, "30 min"], [3600, "1 h"]];
const DETECTOR_RANGES = {
  deauth_min_bursts: [1, 100, true], deauth_window_s: [30, 86400, true], evil_twin_k: [1, 50, false],
  evil_twin_min_db: [1, 60, false], sensor_offline_min: [1, 1440, true],
};

export function detectorsDraft(d) {
  return Object.fromEntries([...Object.keys(DETECTOR_RANGES).map((k) => [k, String(d[k])]),
                             ["evil_twin_guard_s", d.evil_twin_guard_s]]);
}

export function detectorsBody(d) {
  const out = { evil_twin_guard_s: Number(d.evil_twin_guard_s) };
  for (const k of Object.keys(DETECTOR_RANGES)) out[k] = Number(d[k]);
  return out;
}

export function detectorsProblem(d) {
  for (const [k, [lo, hi, whole]] of Object.entries(DETECTOR_RANGES)) {
    const v = Number(d[k]);
    if (d[k] === "" || Number.isNaN(v) || v < lo || v > hi || (whole && !Number.isInteger(v))) {
      return `${k.replaceAll("_", " ")}: ${whole ? "a whole number" : "a number"} from ${lo} to ${hi}`;
    }
  }
  return null;
}

// --- sensors: channel plans ---------------------------------------------------------------------

// Kismet 2025-09's hop stride (install_sensor.sh hop_stride_ok, writes.py
// hop_stride): the first s >= 4 with n % floor(n / s) != 0; only n / gcd(n, s)
// entries are visited
export function hopStride(n) {
  let s = 4;
  if (s > n) s = 1;
  while (n % Math.floor(n / s) === 0) {
    if (s >= n - 1) {
      s = 1;
      break;
    }
    s += 1;
  }
  const gcd = (a, b) => (b ? gcd(b, a % b) : a);
  return { stride: s, gcd: gcd(n, s), visited: n / gcd(n, s) };
}

export const PLANS = [
  { key: "all_89", title: "All channels and widths · 89 settings",
    desc: "also captures data frames of 40/80 MHz networks" },
  { key: "ht20_37", title: "20 MHz only · 37 settings",
    desc: "returns to each channel faster, enough for beacons and deauth; without 144 (no AP has used it, not an ETSI channel)" },
  { key: "locked", title: "Locked to one channel", desc: "for experiments: sees everything on that channel, nothing elsewhere" },
  { key: "custom", title: "Custom list", desc: "your own list of Kismet entries (6, 6HT40+, 36VHT80, …)" },
];
export const DWELLS = [[0.2, "0.2 s (5 hops/s)"], [0.5, "0.5 s (2 hops/s)"], [1, "1 s"]];
export const POLLS = [15, 30, 60];

const PLAN_TEXT = {
  auto: "Kismet's autodetected list", all_89: "89 settings (20/40/80 MHz)", ht20_37: "37 settings (20 MHz only)",
  locked: "locked", custom: "custom list",
};

// "89 settings (20/40/80 MHz)", "locked to channel 6", "custom list · 14 settings"
export function planText(c) {
  if (!c) return "–";
  if (c.channel_plan === "locked") return `locked to channel ${c.channels?.[0] ?? "?"}`;
  if (c.channel_plan === "custom" || c.channel_plan === "auto") {
    return `${PLAN_TEXT[c.channel_plan]} · ${fmtInt(c.n_channels)} settings`;
  }
  return PLAN_TEXT[c.channel_plan] ?? c.channel_plan;
}

// the list a plan sends, or {error}
export function planList(plan, plans, lock, customText) {
  if (plan === "all_89") return { list: plans.all_89 };
  if (plan === "ht20_37") return { list: plans.ht20_37 };
  if (plan === "locked") return plans.lock.includes(lock) ? { list: [lock] } : { error: "pick a channel" };
  const list = (customText ?? "").split(/[\s,]+/).map((c) => c.trim().toUpperCase()).filter(Boolean);
  if (!list.length) return { error: "enter the channels, comma separated" };
  const bad = list.find((c) => !/^\d{1,3}(HT40[+-]|VHT80|VHT160)?$/.test(c));
  if (bad) return { error: `${bad} is not a Kismet entry (e.g. 6, 6HT40+, 36VHT80)` };
  const twice = list.find((c, i) => list.indexOf(c) !== i);
  if (twice) return { error: `${twice} twice` };
  const h = hopStride(list.length);
  if (h.gcd !== 1) {
    return { error: `${list.length} entries: Kismet hops them with stride ${h.stride} and would visit only ${h.visited} - `
      + "add or remove one (a prime length always works)" };
  }
  return { list };
}

// "cycle ≈ 17.8 s"
export function cycleText(n, dwell) {
  return `cycle ≈ ${(Math.round(n * dwell * 10) / 10).toFixed(1)} s`;
}

// the configuration column (docs/config-delivery.md 8): applied, or the state
// of a pending version - refused, applying, delivered, waiting, not accepted
export function configState(s, tz) {
  const p = s.pending;
  if (p) {
    const v = `v${p.version}`;
    const att = s.delivery?.attempt && s.delivery.attempt.version === p.version ? s.delivery.attempt.result : null;
    if (p.refused_at) return { cls: "bad", text: `Refused ${v} - ${p.refused_reason ?? "no reason given"}`,
                               title: "the sensor did not keep this configuration; the previous one runs" };
    if (att === "applying") return { cls: "warn", text: `Applying ${v} - verifying capture, up to 3 min` };
    if (p.delivered_at) return { cls: "warn", text: `Delivered ${v} - waiting for the sensor's result` };
    if (!s.delivery?.accepting) {
      return { cls: "warn", text: `Pending ${v} - this sensor does not accept configuration (CONFIG_APPLY=0)`,
               title: "set CONFIG_APPLY=1 in the sensor's uploader.conf to have it applied" };
    }
    return { cls: "warn", text: `Pending ${v} - waiting for the sensor's next upload` };
  }
  if (s.current?.confirmed_at) {
    return { cls: "ok", text: `Applied ${s.current.version ? `v${s.current.version} ` : ""}since ${fmtDateTime(s.current.confirmed_at, tz)}` };
  }
  return { cls: "neu", text: "none recorded" };
}

// --- sensors: the adapter (read-only) and hardware changes (by hand) ---------------------------------

// the adapter line: "RTL8821CU USB adapter · 0bda:c811 · MAC on record · rtw88_8821cu on wlan1 · 2.4 / 5 GHz";
// the identity is VID:PID + MAC (the MAC itself never leaves the database), the interface is display
// only; reported_by says who knows it
export function adapterLine(a) {
  if (!a) return "no adapter recorded";
  const via = a.driver ? `${a.driver}${a.interface ? ` on ${a.interface}` : ""}` : a.interface;
  return [a.name, a.usb_id ?? "VID:PID not reported yet", a.mac_known ? "MAC on record" : null, via,
          a.bands?.length ? `${a.bands.join(" / ")} GHz` : null].filter(Boolean).join(" · ");
}
export function adapterSource(a) {
  if (!a) return null;
  return a.reported_by === "sensor" ? "reported by the sensor" : "recorded by hand - the sensor will report it itself in the live-upload phase";
}

const pad = (n) => String(n).padStart(2, "0");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "2026-10-12T18:00" (a datetime-local value, wall time in the account's
// zone) -> the UTC instant as ISO; null when empty or not a time
export function zonedToUtc(value, tz) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value ?? "");
  if (!m) return null;
  const wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  // the zone's offset at the guess, twice (DST edges)
  let t = wall;
  for (let i = 0; i < 2; i += 1) {
    const p = partsOf(t, tz);
    const shown = Date.UTC(p.year, MONTHS.indexOf(p.month), p.day, Number(p.hour), Number(p.minute));
    t = wall - (shown - t);
  }
  return new Date(t).toISOString().replace(".000Z", "Z");
}

// the datetime-local value of an instant in the account's zone
export function utcToZoned(v, tz) {
  const p = partsOf(v, tz);
  return `${p.year}-${pad(MONTHS.indexOf(p.month) + 1)}-${pad(p.day)}T${p.hour}:${p.minute}`;
}

// the body of "Add hardware change": when (not in the future) and an optional note
export function hardwareChangeProblem(sinceUtc, now = Date.now()) {
  if (!sinceUtc) return "give the date and time of the change";
  if (Date.parse(sinceUtc) > now + 5 * 60e3) return "a change cannot lie in the future";
  return null;
}
export function hardwareChangeBody(sinceUtc, note) {
  return { valid_from: sinceUtc, note: note?.trim() || null };
}

// --- sensors: add, edit, rename, delete -------------------------------------------------------------

// a sensor's name: a short handle (URLs, the importer's --sensor), as api writes.py sensor_name()
export function sensorNameProblem(name, sensors, current = null) {
  const n = (name ?? "").trim();
  if (!n) return "give the sensor a name";
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/.test(n)) return "1 to 40 characters: letters, digits, . _ - (not first)";
  if (sensors.some((x) => x.name === n && x.name !== current)) return `a sensor named ${n} exists already`;
  return null;
}

// the body of add / edit: a blank location or description clears it
export function sensorBody(draft) {
  return { name: draft.name.trim(), location: draft.location.trim() || null, description: draft.description.trim() || null };
}

// the importer files data under the sensor's name (--sensor NAME): a rename
// must be followed there, or the next import creates a sensor with the old name
export function renameNote(oldName, newName) {
  return `Renamed ${oldName} to ${newName}. The importer files data under the name: use --sensor ${newName} from now on, `
    + `or the next import creates a new sensor ${oldName}.`;
}

// Delete: never a sensor with data, and the name typed to confirm
export function deleteGuard(meta, name, typed) {
  if (meta?.has_polls) return { allowed: false, why: `${name} has collected data - a sensor with data is never deleted` };
  if (typed.trim() !== name) return { allowed: false, why: `type ${name} to confirm`, ask: true };
  return { allowed: true, ask: true };
}

// --- the history --------------------------------------------------------------------------------

// rows of the configuration history: valid from, sensor, change, effect on the data
export function historyRows(history, tz) {
  return (history ?? []).map((h, i) => ({
    key: `${h.ts}|${h.sensor}|${i}`, ts: fmtDateTime(h.ts, tz), sensor: h.sensor, label: h.label,
    boundary: h.boundary,
    effect: h.kind === "hardware_change" ? "RSSI baselines start again"
      : h.kind === "coverage" ? "tried and rolled back - no boundary" : h.boundary ? "evaluated apart" : "no boundary",
  }));
}
