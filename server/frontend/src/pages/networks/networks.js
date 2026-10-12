// The texts of the Networks page (boards/Networks.dc.html) from the api's
// rows - pure, tested in networks.test.js.

import { fmtDay, fmtDuration, fmtInt, fmtTime } from "../../format.js";

// "Ruckus Wireless" -> "Ruckus", "TP-LINK TECHNOLOGIES CO.,LTD." -> "TP-LINK",
// "Apple, Inc." -> "Apple": the vendor as the boards name it
const NOISE = /\b(wireless|technologies|technology|networks?|communications?|corporation|corp\.?|co\.?|ltd\.?|inc\.?|electronics|international|gmbh|s\.?a\.?|limited|company)\b\.?/gi;
export function shortVendor(manuf) {
  if (!manuf) return null;
  const head = manuf.split(",")[0].replace(NOISE, " ").replace(/\s+/g, " ").trim();
  return head || manuf.trim();
}

export function vendorList(manufs) {
  const out = [];
  for (const m of manufs ?? []) {
    const v = shortVendor(m);
    if (v && !out.includes(v)) out.push(v);
  }
  return out.length ? out.join(", ") : "–";
}

const MFP_TEXT = { no: "no MFP", capable: "MFP only capable", required: "MFP required", unknown: "MFP unknown" };

// the managed table's "Actual": "WPA2-Enterprise, MFP only capable" - labels
// and MFP of the BSSIDs with a beacon record (the others are counted as "not
// assessed" under the compliance badge); no MFP part for open/WEP networks,
// they have nothing it could protect
export function actualText(actual) {
  const labels = actual?.labels ?? [];
  if (!labels.length) return actual?.unknown ? "Unknown" : "–";
  const mfp = actual.mfp ?? [];
  const m = labels.every((l) => l === "Open" || l === "WEP") ? null
    : mfp.length === 1 ? MFP_TEXT[mfp[0]] ?? mfp[0] : mfp.length > 1 ? "MFP mixed" : null;
  return m ? `${labels.join(" / ")}, ${m}` : labels.join(" / ");
}

const NOT_ASSESSED = "security unknown (no beacon record): not compared with the policy";

// "Compliant" / "Not met 2/28" / "Not seen" / "Not assessed"; a BSSID of
// unknown security is not assessed - counted beside the badge, never failing
// (decided 2026-10-12)
export function complianceBadge(n) {
  const c = n.compliance;
  if (!c.total && c.not_assessed) return { cls: "neu", text: "Not assessed", title: NOT_ASSESSED };
  if (!c.total) return { cls: "neu", text: "Not seen", title: "no BSSID of this network was seen in the period" };
  if (c.met) return { cls: "ok", text: "Compliant", title: "every assessed BSSID meets the policy" };
  return { cls: "warn", text: `Not met ${c.failing}/${c.total}`, title: c.reason };
}

// "2 not assessed" under the badge (only with assessed BSSIDs too)
export function notAssessedText(n) {
  const c = n.compliance;
  return c.total && c.not_assessed ? { text: `${fmtInt(c.not_assessed)} not assessed`, title: NOT_ASSESSED } : null;
}

// "31 (29 active)"
export function bssidCount(n) {
  return `${fmtInt(n.bssids)} (${fmtInt(n.bssids_active)} active)`;
}

const LEVEL_BADGE = { open: "bad", legacy: "bad", psk_no_mfp: "warn", strong: "ok", unknown: "neu" };
const LEVEL_ORDER = ["open", "legacy", "psk_no_mfp", "strong", "unknown"];

// one badge for a network's security: colored by its weakest level, the
// labels of its BSSIDs as text
export function securityBadge(sec) {
  const labels = sec?.labels ?? [];
  const weakest = sec?.weakest && LEVEL_ORDER.includes(sec.weakest) ? sec.weakest : "unknown";
  return { cls: LEVEL_BADGE[weakest], text: labels.length ? labels.join(" / ") : "Unknown" };
}

export function levelBadge(level) {
  return LEVEL_BADGE[level] ?? "neu";
}

// a lifetime: "35 min"; a BSSID seen in a single poll has 0 s -> "under 1 min"
export function lifetimeText(s) {
  return s != null && s < 60 ? "under 1 min" : fmtDuration(s);
}

// "transient, median sighting 3 min": a sighting is a run of observations
// without a gap over 10 minutes (decided 2026-10-12)
export function transientText(medianS) {
  return medianS == null ? "transient" : `transient, median sighting ${lifetimeText(medianS)}`;
}

// presence (SPEC Q9): permanent / daytime only / transient, median sighting 3 min / intermittent
export function presenceText(row) {
  if (row.presence === "daytime") return "daytime only";
  if (row.presence === "transient") return transientText(row.median_sighting_s);
  return row.presence;
}

// "hidden:EC:58:EA:95:89:DC" -> the BSSID of a hidden network without a known name
export function hiddenBssid(key) {
  return key && key.startsWith("hidden:") ? key.slice(7) : null;
}

export function networkName(name, key) {
  return name ?? (hiddenBssid(key) ? "hidden network" : key);
}

const KIND = { downgrade: { label: "Downgrade", cls: "bad" }, upgrade: { label: "Upgrade", cls: "ok" },
               change: { label: "Change", cls: "neu" } };

// "Security changes in the period": rows and the summary line
export function changeRows(changes, tz) {
  return changes.map((c) => ({
    kind: KIND[c.kind] ?? KIND.change,
    when: `${fmtDay(c.ts, tz)} ${fmtTime(c.ts, tz)}`,
    network: networkName(hiddenBssid(c.network) ? null : c.network, c.network),
    bssid: c.bssids.length > 1 ? `${c.bssids[0]} + ${c.bssids.length - 1}` : c.bssids[0],
    what: `${c.from} → ${c.to}`,
  }));
}

export function changeSummary(changes) {
  if (!changes.length) return "no changes in this period";
  const downs = changes.filter((c) => c.kind === "downgrade").length;
  return `${changes.length} ${changes.length === 1 ? "change" : "changes"}, ${downs} ${downs === 1 ? "downgrade" : "downgrades"}`;
}

// heatmap: the ramp step of a value (the api's six thresholds, e.g. 10 30 70 110 160 200)
export function rampIndex(v, ramp) {
  let i = 0;
  while (i < ramp.length && v >= ramp[i]) i += 1;
  return i;              // 0 .. ramp.length (7 colors)
}

// the legend: the lower bound of each color, the last one open ("0 10 30 ... 200+")
export function rampLabels(ramp) {
  return [0, ...ramp].map((t, i) => (i === ramp.length ? `${fmtInt(t)}+` : fmtInt(t)));
}

const WEEKDAY_PLURAL = { Mon: "Mondays", Tue: "Tuesdays", Wed: "Wednesdays", Thu: "Thursdays", Fri: "Fridays",
                         Sat: "Saturdays", Sun: "Sundays" };

const two = (n) => String(n).padStart(2, "0");

// the hint line of a heatmap cell (Networks board)
export function cellText(view, row, h, cell) {
  const span = `${two(h)}:00–${two((h + 1) % 24)}:00`;
  if (view === "week") {
    const head = `${WEEKDAY_PLURAL[row.label] ?? row.label}, ${span}`;
    return cell.state === "ok"
      ? `${head} · average ${fmtInt(cell.value)} clients over ${cell.days} ${cell.days === 1 ? "day" : "days"}`
      : `${head} · no data in the period`;
  }
  const head = `${row.label}, ${span}`;
  if (cell.state === "outside") return `${head} · outside the period`;
  if (cell.state === "no_data") return `${head} · no sensor collecting`;
  if (cell.state === "load_missing") return `${head} · client counts were not recorded`;
  const split = Object.entries(cell.by_network ?? {}).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k} ${fmtInt(v)}`).join(", ");
  return `${head} · ${fmtInt(cell.value)} clients${split ? ` (${split})` : ""}`;
}
