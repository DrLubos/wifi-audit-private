// The global filters (SPEC section 3): ?sensor=all|<name>&range=24h|7d|30d.
// The URL is the source of truth; a missing key comes from the last choice
// (localStorage) or the default, an invalid one becomes the default, and the
// completed query is written back with replace so the URL is always
// shareable. Pure part of useFilters.js (tested in filters.test.js).

export const RANGES = ["24h", "7d", "30d"];
export const DEFAULTS = { sensor: "all", range: "7d" };
export const STORAGE_KEY = "wifi-audit.filters.v1";

function valid(key, value, sensorNames) {
  if (value == null || value === "") return false;
  if (key === "range") return RANGES.includes(value);
  if (key === "sensor") return value === "all" || !sensorNames || sensorNames.includes(value);
  return true;
}

// keys: the filters the page uses; get(k): the URL's value; stored: the last
// choice or null; sensorNames: the account's sensors, null while loading.
// -> {values: {sensor, range}, changed}: changed = the URL must be rewritten
export function resolveFilters(keys, get, stored, sensorNames) {
  const values = { ...DEFAULTS };
  let changed = false;
  for (const k of keys) {
    const fromUrl = get(k);
    let v = fromUrl;
    if (!valid(k, v, sensorNames)) v = stored && valid(k, stored[k], sensorNames) ? stored[k] : DEFAULTS[k];
    values[k] = v;
    if (v !== fromUrl) changed = true;
  }
  return { values, changed };
}

// ?sensor=…&range=… for links between pages: the global keys only
export function filterSearch(values) {
  const parts = Object.keys(DEFAULTS).filter((k) => values[k]).map((k) => `${k}=${encodeURIComponent(values[k])}`);
  return `?${parts.join("&")}`;
}

export function readStored(storage) {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    const v = raw ? JSON.parse(raw) : null;
    return v && typeof v === "object" ? v : null;
  } catch {
    return null;
  }
}

export function writeStored(storage, values) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify({ sensor: values.sensor, range: values.range }));
  } catch {
    // private window, blocked storage: the URL still carries the filters
  }
}
