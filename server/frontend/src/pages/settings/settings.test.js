import { test, eq, done } from "../../testing/tiny.js";
import {
  addAllowed, captureText, classificationBody, classificationDraft, classificationProblem, configState, cycleText,
  detectorsBody, detectorsDraft, detectorsProblem, historyRows, hopStride, hwDetail, hwText, networkDraft,
  networksBody, ouiText, parseAllowed, planList, planText, removeAllowed, sameBody, ssidProblem, utcToZoned, utf8Length,
  zonedToUtc,
} from "./settings.js";

const TZ = "Europe/Bratislava";
const PLANS = { all_89: Array.from({ length: 89 }, (_, i) => String(i)), ht20_37: Array.from({ length: 37 }, (_, i) => String(i)),
                lock: ["1", "6", "11", "36"] };

test("networks: drafts, the body, allowed entries", () => {
  const api = { id: 3, ssid: "IK-WIFI", kind: "managed", expected_security: "open_allowed", require_mfp: false,
                allowed_bssids: [], allowed_ouis: ["EC:58:EA", "3C:46:A1"], note: null };
  const d = networkDraft(api);
  const saved = networksBody([d]);
  eq(saved.networks[0], { id: 3, ssid: "IK-WIFI", kind: "managed", expected_security: "open_allowed", require_mfp: false,
                          allowed_bssids: [], allowed_ouis: ["EC:58:EA", "3C:46:A1"], note: null });
  eq(parseAllowed("f0-6f-ce-41-1f-b0"), { kind: "bssid", value: "F0:6F:CE:41:1F:B0" });
  eq(parseAllowed(" f06fce "), { kind: "oui", value: "F0:6F:CE" });
  eq(Boolean(parseAllowed("F0:6F").error), true);
  const d2 = addAllowed(d, parseAllowed("F0:6F:CE"));
  eq(d2.allowed_ouis, ["3C:46:A1", "EC:58:EA", "F0:6F:CE"]);
  eq(addAllowed(d2, parseAllowed("f06fce")) === d2, true);                // once
  eq(sameBody(networksBody([d2]), saved), false);
  eq(sameBody(networksBody([removeAllowed(d2, "oui", "F0:6F:CE")]), networksBody([networkDraft({ ...api, allowed_ouis: ["3C:46:A1", "EC:58:EA"] })])), true);
  // a friendly network is not checked
  const f = networksBody([{ ...d, kind: "friendly", require_mfp: true }]).networks[0];
  eq([f.expected_security, f.require_mfp], [null, false]);
  eq(networkDraft({ ssid: "new" }).id, null);
  eq(ouiText("EC:58:EA", { "EC:58:EA": "Ruckus" }), "Ruckus EC:58:EA");
  eq(ssidProblem("IK-WIFI", [d]), "IK-WIFI is in the list already");
  eq(ssidProblem("x".repeat(33), [d]), "an SSID has at most 32 bytes");
  eq(ssidProblem("eduroam", [d]), null);
  eq([utf8Length("Domčo"), utf8Length("😀")], [6, 4]);
});

test("classification and detectors", () => {
  const c = classificationDraft({ ignore_random_bssid: true, suspicious_unallowed: true, managed_by_vendor: false,
                                  ignore_weaker_dbm: null, mobile_vendors: ["Apple", "Samsung"] });
  eq(classificationBody(c), { ignore_random_bssid: true, suspicious_unallowed: true, managed_by_vendor: false,
                              ignore_weaker_dbm: null, mobile_vendors: ["Apple", "Samsung"] });
  eq(classificationBody({ ...c, weaker_on: true, weaker_dbm: "-92", vendors: "Apple, ,Google " }).ignore_weaker_dbm, -92);
  eq(classificationBody({ ...c, vendors: "Apple, ,Google " }).mobile_vendors, ["Apple", "Google"]);
  eq(classificationProblem({ ...c, weaker_on: true, weaker_dbm: "-20" }) != null, true);
  const d = detectorsDraft({ deauth_min_bursts: 3, deauth_window_s: 600, evil_twin_k: 6, evil_twin_min_db: 8,
                             evil_twin_guard_s: 0, sensor_offline_min: 15 });
  eq(detectorsBody(d), { evil_twin_guard_s: 0, deauth_min_bursts: 3, deauth_window_s: 600, evil_twin_k: 6,
                         evil_twin_min_db: 8, sensor_offline_min: 15 });
  eq(detectorsProblem(d), null);
  eq(detectorsProblem({ ...d, deauth_min_bursts: "2.5" }), "deauth min bursts: a whole number from 1 to 100");
  eq(detectorsProblem({ ...d, sensor_offline_min: "" }) != null, true);
});

test("channel plans and the stride rule", () => {
  eq(hopStride(89), { stride: 4, gcd: 1, visited: 89 });
  eq(hopStride(90), { stride: 4, gcd: 2, visited: 45 });
  eq(hopStride(4).gcd, 1);
  eq(planList("all_89", PLANS).list.length, 89);
  eq(planList("locked", PLANS, "6"), { list: ["6"] });
  eq(planList("locked", PLANS, "").error, "pick a channel");
  eq(planList("custom", PLANS, null, "1, 6 11,36ht40+"), { list: ["1", "6", "11", "36HT40+"] });
  const fourteen = Array.from({ length: 13 }, (_, i) => String(i + 1)).concat("36").join(",");
  eq(planList("custom", PLANS, null, fourteen).error,
     "14 entries: Kismet hops them with stride 4 and would visit only 7 - add or remove one (a prime length always works)");
  eq(planList("custom", PLANS, null, "1,1").error, "1 twice");
  eq(planList("custom", PLANS, null, "ch6").error, "CH6 is not a Kismet entry (e.g. 6, 6HT40+, 36VHT80)");
  eq(cycleText(89, 0.2), "cycle ≈ 17.8 s");
  eq(planText({ channel_plan: "all_89", n_channels: 89 }), "89 settings (20/40/80 MHz)");
  eq(planText({ channel_plan: "locked", channels: ["6"], n_channels: 1 }), "locked to channel 6");
  eq(planText({ channel_plan: "custom", n_channels: 90 }), "custom list · 90 settings");
  eq(configState({ current: { confirmed_at: "2026-09-27T13:04:57Z" }, pending: null }, TZ).text, "In effect since 27 Sep 15:04");
  eq(configState({ current: null, pending: { version: 4 } }, TZ).text, "Saved, not delivered");
});

test("hardware texts and the account's wall time", () => {
  const catalog = [{ id: 1, kind: "adapter", name: "RTL8821CU USB adapter", gain_dbi: null, bands: ["2.4", "5"], connector: "USB" },
                   { id: 2, kind: "antenna", name: "Dipole", gain_dbi: 5, bands: ["2.4", "5"], connector: "RP-SMA" }];
  eq(hwText(catalog[1]), "Dipole (5 dBi)");
  eq(hwDetail(catalog[1]), "2.4 / 5 GHz · RP-SMA");
  eq(captureText({ adapter_id: 1, antenna_id: 2 }, catalog), "RTL8821CU USB adapter + Dipole (5 dBi)");
  eq(captureText({ adapter_id: 1, antenna_id: null }, catalog), "RTL8821CU USB adapter + its own antenna");
  eq(captureText(null, catalog), "none recorded");
  // CEST (+2), CET (+1) after 25 Oct 2026, and back
  eq(zonedToUtc("2026-10-12T18:00", TZ), "2026-10-12T16:00:00Z");
  eq(zonedToUtc("2026-11-02T08:30", TZ), "2026-11-02T07:30:00Z");
  eq(zonedToUtc("2027-03-29T12:00", TZ), "2027-03-29T10:00:00Z");
  eq(zonedToUtc("garbage", TZ), null);
  eq(utcToZoned("2026-10-12T16:00:00Z", TZ), "2026-10-12T18:00");
  eq(utcToZoned("2026-11-02T07:30:00Z", TZ), "2026-11-02T08:30");
});

test("history rows", () => {
  const rows = historyRows([
    { sensor: "pi-fri", ts: "2026-10-12T16:00:00Z", boundary: true, kind: "hardware", label: "Capture hardware: A + B" },
    { sensor: "pi-fri", ts: "2026-09-27T13:04:57Z", boundary: true, kind: "change", label: "Hop list: 89 entries" },
    { sensor: "pi-fri", ts: "2026-09-27T14:15:50Z", boundary: false, kind: "change", label: "Hop guard" }], TZ);
  eq(rows.map((r) => [r.ts, r.effect]), [["12 Oct 18:00", "RSSI baselines start again"], ["27 Sep 15:04", "evaluated apart"],
                                         ["27 Sep 16:15", "no boundary"]]);
});

done();
