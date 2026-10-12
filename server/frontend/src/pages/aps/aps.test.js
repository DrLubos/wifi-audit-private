import { test, eq, ok, done } from "../../testing/tiny.js";
import {
  apName, bandRect, bssidOfKey, changeParts, changesPerDayText, channelDetail, channelLine, chips, classBadge,
  encryptionText, eventBadges, groupLabel, historyRows, isActive, isBssid, lastSeenText, loadNote, mfpText, pagerText,
  policyBadge, ratingBadge, recommendation, rssiLines, rssiScale, rssiTicks, rssiTip, securityCell, seenByChips,
  statusBadge, utilSummary, vendorText, widthText,
} from "./aps.js";

const TZ = "Europe/Bratislava";

test("list cells", () => {
  eq(classBadge("managed"), { cls: "info", text: "Managed" });
  eq(classBadge("ignored"), { cls: "mute", text: "Ignored" });
  eq(statusBadge(false), { cls: "neu", text: "Inactive" });
  eq(apName({ network: "IK-WIFI", hidden: false, ssid: "IK-WIFI" }), { text: "IK-WIFI", hidden: false });
  eq(apName({ network: null, hidden: true, ssid: "" }), { text: "hidden network", hidden: true });
  eq(vendorText({ manuf: "Ruckus Wireless" }), "Ruckus");
  eq(vendorText({ manuf: null, random_bssid: true }), "– (randomized)");
  eq(securityCell({ security: "open", label: "Open", policy_met: true }),
     { cls: "bad", text: "Open", note: "allowed", title: undefined });
  eq(securityCell({ security: "legacy", label: "WPA1/WPA2 mixed", policy_met: false, policy_reason: "weaker than WPA2-PSK" }),
     { cls: "bad", text: "WPA1/WPA2 mixed", note: "policy not met", title: "weaker than WPA2-PSK" });
  eq(securityCell({ security: "psk_no_mfp", label: "WPA2-PSK", policy_met: null }).note, null);
  eq(seenByChips([{ sensor: "pi-fri", rssi_median: -66.5 }, { sensor: "pi-lab", rssi_median: null }]),
     [{ name: "pi-fri", text: "pi-fri −66" }, { name: "pi-lab", text: "pi-lab" }]);
  eq(pagerText(1, 50, 603, 100, 50), "1–50 of 603 (100 hotspots hidden)");
  eq(pagerText(2, 50, 60, 0, 10), "51–60 of 60");
  eq(pagerText(1, 50, 0, 0, 0), "none");
  const c = chips({ all: 268, managed: 62, friendly: 0, unknown: 133, suspicious: 0, ignored: 73, band_2_4: 136 });
  eq(c.classes.map((x) => x.label), ["All 268", "Managed 62", "Friendly 0", "Unknown 133", "Suspicious 0", "Ignored 73"]);
  eq(c.toggles.map((x) => x.key), ["band24", "band5", "open", "new", "hotspots"]);
  eq(groupLabel("ssid", "hidden:EC:58:EA:95:89:DC", 1), "hidden network EC:58:EA:95:89:DC · 1 AP");
  eq(groupLabel("sensor", "pi-fri", 120), "pi-fri · 120 APs");
});

test("last seen", () => {
  const now = "2026-10-12T10:00:00Z";                    // 12:00 local
  eq(lastSeenText("2026-10-12T09:59:30Z", now, TZ), "now");
  eq(lastSeenText("2026-10-12T09:54:00Z", now, TZ), "6 min ago");
  eq(lastSeenText("2026-10-12T07:00:00Z", now, TZ), "3 h ago");
  eq(lastSeenText("2026-10-11T20:46:41Z", now, TZ), "yesterday");
  eq(lastSeenText("2026-10-08T12:00:00Z", now, TZ), "8 Oct");
  eq(lastSeenText(null, now, TZ), "–");
});

test("device keys of the previous pages", () => {
  eq(bssidOfKey("4202770D00000000_F62CE5D4C3B2"), "B2:C3:D4:E5:2C:F6");
  eq(bssidOfKey("EC:58:EA:14:13:68"), null);
  ok(isBssid("EC:58:EA:14:13:68"));
  ok(!isBssid("4202770D00000000_F62CE5D4C3B2"));
});

test("AP page texts", () => {
  eq([widthText("HT20"), widthText("HT40+"), widthText("VHT80"), widthText(null)], ["20 MHz", "40 MHz", "80 MHz", null]);
  const id = { channel: "48", band: "5", ht_mode: "HT80" };
  eq(channelLine(id), "channel 48 (5 GHz, 80 MHz)");
  eq(channelDetail(id), "48 · 5 GHz · 80 MHz (HT80)");
  eq(channelLine({ channel: null }), "channel unknown");
  eq(encryptionText({ level: "strong", label: "WPA2-Enterprise",
                      detail: { akm: ["802.1X", "802.1X-FT"], pairwise: ["CCMP"] } }),
     "WPA2-Enterprise (802.1X, 802.1X-FT) · CCMP");
  eq(encryptionText({ level: "unknown", label: "Unknown" }), "unknown (no beacon record)");
  eq([mfpText("capable"), mfpText("no"), mfpText(null)], ["capable, not required", "not supported", "unknown"]);
  eq(ratingBadge("psk_no_mfp"), { cls: "warn", text: "WPA2-PSK without MFP" });
  eq(policyBadge(null, "strong"), null);
  eq(policyBadge({ met: false, reason: "MFP not required" }, "strong"), { cls: "warn", text: "Not met", reason: "MFP not required" });
  eq(policyBadge({ met: false, reason: "security unknown" }, "unknown").text, "Not assessed");
  eq(recommendation({ met: false, reason: "MFP not required" }, "strong"),
     "Require MFP, otherwise clients stay exposed to forged deauthentication.");
  eq(recommendation({ met: true }, "strong"), null);
  const now = "2026-10-11T19:41:41Z";
  eq(loadNote({ qbss_stations: 0, util: 11.4, at: "2026-10-11T19:41:11Z", sensor: "pi-fri" }, now),
     "Utilization and station count come from the AP's own BSS Load element, measured by pi-fri 30 s ago.");
  eq(loadNote({ qbss_stations: null, util: null, at: "2026-10-11T16:41:41Z", sensor: "pi-fri" }, now),
     "This AP sends no BSS Load element: clients are the stations the sensor saw, measured by pi-fri 3 h ago.");
  eq(changeParts({ field: "adv_channel", old: "64", new: "48" }), { what: "Channel", from: "64", to: "48" });
  eq(changeParts({ field: "mfp_sup", old: "false", new: "true" }), { what: "MFP capable", from: "no", to: "yes" });
  eq(changesPerDayText(11.5), "changes channel ~12× per day (dynamic channel selection)");
  eq(changesPerDayText(1.4), "changes channel ~1× per day");
  eq(changesPerDayText(0.2), null);
  const rows = historyRows({ rows: [{ ts: "2026-10-11T19:25:41Z", sensor: "pi-fri",
                                      changes: [{ field: "adv_channel", old: "5", new: "8" }] }] },
                          [{ sensor: "pi-fri", ts: "2026-09-15T14:55:17Z" }], TZ);
  eq(rows.map((r) => [r.when, r.first ?? false]), [["11 Oct 21:25", false], ["15 Sep 16:55", true]]);
  eq(eventBadges({ severity: "low", status: "false_positive" }),
     { severity: { cls: "neu", text: "Low" }, status: { cls: "info", text: "False positive" } });
  eq(eventBadges({ severity: "high", status: "open" }).status, null);
  const acc = [{ name: "pi-fri", last_capture_at: "2026-10-11T20:46:41Z" }, { name: "pi-lab", last_capture_at: null }];
  ok(isActive("2026-10-11T20:43:00Z", [{ name: "pi-fri" }], acc));
  ok(!isActive("2026-10-11T19:41:11Z", [{ name: "pi-fri" }], acc));
  ok(!isActive("2026-10-11T20:46:41Z", [{ name: "pi-lab" }], acc));
});

test("RSSI chart", () => {
  const sc = rssiScale({ "pi-fri": [-82, -84, null, -79] }, { "pi-fri": { median: -82, robust_sd: 3 } });
  eq(sc, { top: -65, bottom: -95, ticks: [-65, -75, -85, -95] });         // -76 .. -88 widened to 30 dB around it
  eq(rssiScale({}, {}).top, -50);
  eq(rssiLines([-70, -85, null, -100], -70, -100, 220), ["125.0,0.0 375.0,110.0", "875.0,220.0 878,220.0"]);
  const br = bandRect({ median: -85, robust_sd: 2.5 }, -70, -100, 220);     // -80 .. -90 dBm
  eq([br.y.toFixed(2), br.height.toFixed(2)], ["73.33", "73.33"]);
  const d = { t: [1760209200], series: { "pi-fri": [-90.5], "pi-lab": [null] }, n_floor: { "pi-lab": [3] },
              baseline: { "pi-fri": { median: -82, robust_sd: 2 } } };
  const tip = rssiTip(0, d, { "pi-fri": "var(--c1)", "pi-lab": "var(--c2)" }, TZ);
  eq(tip.head, "Sat 11 Oct, 21:00–22:00");
  eq(tip.rows, [{ c: "var(--c1)", v: "−90 dBm", l: "pi-fri" }, { c: "var(--c2)", v: "–", l: "pi-lab: floor readings only" }]);
  eq(tip.notes, ["pi-fri: outside its baseline band (−78 dBm to −86 dBm)"]);
  const t = Array.from({ length: 48 }, (_, i) => 1760220000 + i * 3600);       // from Sun 12 Oct 00:00 local
  eq(rssiTicks(t, TZ).map((x) => x.t), ["Sun 12 Oct", "Mon 13 Oct"]);
  eq(utilSummary([10, null, 20, 30], [15, null, 40, 35], ["a", "b", "c", "d"]), "Median 20 %, peak 40 % (c).");
  eq(utilSummary([null], [null], ["a"]), null);
});

done();
