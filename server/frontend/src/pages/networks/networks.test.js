import { test, eq, done } from "../../testing/tiny.js";
import {
  actualText, bssidCount, cellText, changeRows, changeSummary, complianceBadge, lifetimeText, networkName,
  notAssessedText, presenceText, rampIndex, rampLabels, securityBadge, shortVendor, vendorList,
} from "./networks.js";

const TZ = "Europe/Bratislava";

test("vendors as the boards name them", () => {
  eq(shortVendor("Ruckus Wireless"), "Ruckus");
  eq(shortVendor("TP-LINK TECHNOLOGIES CO.,LTD."), "TP-LINK");
  eq(shortVendor("Apple, Inc."), "Apple");
  eq(shortVendor("Routerboard.com"), "Routerboard.com");
  eq(shortVendor(null), null);
  eq(vendorList(["Ruckus Wireless", "Ruckus Wireless", "Apple, Inc."]), "Ruckus, Apple");
  eq(vendorList([]), "–");
});

test("managed table texts", () => {
  eq(actualText({ labels: ["WPA2-Enterprise"], mfp: ["capable"], unknown: 0 }), "WPA2-Enterprise, MFP only capable");
  eq(actualText({ labels: ["WPA2-Enterprise"], mfp: ["no"], unknown: 2 }), "WPA2-Enterprise, no MFP");
  eq(actualText({ labels: ["Open"], mfp: ["no"], unknown: 3 }), "Open");
  eq(actualText({ labels: ["Open", "WPA2-PSK"], mfp: ["no"], unknown: 0 }), "Open / WPA2-PSK, no MFP");
  eq(actualText({ labels: [], mfp: [], unknown: 1 }), "Unknown");
  eq(actualText({ labels: [], mfp: [], unknown: 0 }), "–");
  eq(actualText({ labels: ["WPA2-PSK"], mfp: ["no", "required"] }), "WPA2-PSK, MFP mixed");
  eq(complianceBadge({ compliance: { met: false, failing: 2, total: 28, reason: "security unknown" } }),
     { cls: "warn", text: "Not met 2/28", title: "security unknown" });
  eq(complianceBadge({ compliance: { met: true, failing: 0, total: 27 } }).text, "Compliant");
  eq(complianceBadge({ compliance: { met: false, failing: 0, total: 0 } }).text, "Not seen");
  // unknown security: not assessed, beside the badge (decided 2026-10-12)
  const dot1x = { compliance: { met: true, failing: 0, total: 26, not_assessed: 2, reason: null } };
  eq(complianceBadge(dot1x).text, "Compliant");
  eq(notAssessedText(dot1x).text, "2 not assessed");
  eq(notAssessedText({ compliance: { met: true, failing: 0, total: 26, not_assessed: 0 } }), null);
  const none = { compliance: { met: false, failing: 0, total: 0, not_assessed: 3 } };
  eq(complianceBadge(none).text, "Not assessed");
  eq(notAssessedText(none), null);
  eq(bssidCount({ bssids: 31, bssids_active: 29 }), "31 (29 active)");
});

test("security badge by the weakest level", () => {
  eq(securityBadge({ labels: ["Open", "Unknown"], weakest: "open" }), { cls: "bad", text: "Open / Unknown" });
  eq(securityBadge({ labels: ["WPA2-PSK"], weakest: "psk_no_mfp" }), { cls: "warn", text: "WPA2-PSK" });
  eq(securityBadge({ labels: [], weakest: null }), { cls: "neu", text: "Unknown" });
});

test("presence and names", () => {
  eq(presenceText({ presence: "daytime" }), "daytime only");
  eq(presenceText({ presence: "transient", median_sighting_s: 210 }), "transient, median sighting 3 min");
  eq(presenceText({ presence: "transient", median_sighting_s: 0 }), "transient, median sighting under 1 min");
  eq(presenceText({ presence: "transient", median_sighting_s: null }), "transient");
  eq(lifetimeText(59), "under 1 min");
  eq(lifetimeText(60), "1 min");
  eq(lifetimeText(null), "–");
  eq(presenceText({ presence: "permanent" }), "permanent");
  eq(networkName(null, "hidden:EC:58:EA:95:89:DC"), "hidden network");
  eq(networkName("kram", "kram"), "kram");
});

test("security changes", () => {
  const rows = changeRows([
    { ts: "2026-10-06T14:20:00Z", kind: "downgrade", network: "C", bssids: ["64:09:80:5E:11:90"], from: "WPA2-PSK", to: "Open" },
    { ts: "2026-10-06T07:05:00Z", kind: "upgrade", network: "A", bssids: ["A0:AB:1B:30:7C:E2", "x", "y", "z"], from: "WPA2-PSK", to: "WPA3-SAE" },
    { ts: "2026-10-05T10:00:00Z", kind: "change", network: "hidden:AA:BB:CC:DD:EE:FF", bssids: ["AA:BB:CC:DD:EE:FF"], from: "MFP no", to: "MFP capable" },
  ], TZ);
  eq(rows.map((r) => [r.kind.label, r.when, r.network, r.bssid, r.what]), [
    ["Downgrade", "Tue 6 Oct 16:20", "C", "64:09:80:5E:11:90", "WPA2-PSK → Open"],
    ["Upgrade", "Tue 6 Oct 09:05", "A", "A0:AB:1B:30:7C:E2 + 3", "WPA2-PSK → WPA3-SAE"],
    ["Change", "Mon 5 Oct 12:00", "hidden network", "AA:BB:CC:DD:EE:FF", "MFP no → MFP capable"],
  ]);
  eq(changeSummary([{ kind: "downgrade" }, { kind: "upgrade" }, { kind: "upgrade" }]), "3 changes, 1 downgrade");
  eq(changeSummary([]), "no changes in this period");
});

test("heatmap ramp and hint", () => {
  const ramp = [25, 75, 150, 250, 350, 450];
  eq([0, 24, 25, 149, 450, 900].map((v) => rampIndex(v, ramp)), [0, 0, 1, 2, 6, 6]);
  const own = [10, 30, 70, 110, 160, 200];                 // from a 30-day peak of 199
  eq([0, 9, 10, 112, 199, 200].map((v) => rampIndex(v, own)), [0, 0, 1, 4, 5, 6]);
  eq(rampLabels(own), ["0", "10", "30", "70", "110", "160", "200+"]);
  const row = { label: "Sat 10 Oct" };
  eq(cellText("days", row, 9, { state: "ok", value: 68, by_network: { "IK-WIFI": 53, "IK-WIFI-DOT1X": 15, FRI_wifi: 0 } }),
     "Sat 10 Oct, 09:00–10:00 · 68 clients (IK-WIFI 53, IK-WIFI-DOT1X 15, FRI_wifi 0)");
  eq(cellText("days", row, 23, { state: "outside" }), "Sat 10 Oct, 23:00–00:00 · outside the period");
  eq(cellText("days", row, 3, { state: "load_missing" }), "Sat 10 Oct, 03:00–04:00 · client counts were not recorded");
  eq(cellText("week", { label: "Mon" }, 10, { state: "ok", value: 40, days: 1 }),
     "Mondays, 10:00–11:00 · average 40 clients over 1 day");
  eq(cellText("week", { label: "Sun" }, 0, { state: "no_data" }), "Sundays, 00:00–01:00 · no data in the period");
});

done();
