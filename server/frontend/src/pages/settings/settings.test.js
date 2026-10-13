import { test, eq, done } from "../../testing/tiny.js";
import {
  adapterLine, adapterSource, deleteGuard, hardwareChangeBody, hardwareChangeProblem, renameNote, sensorBody,
  sensorNameProblem,
  addAllowed, classificationBody, classificationDraft, classificationProblem, configState, cycleText,
  detectorsBody, detectorsDraft, detectorsProblem, historyRows, hopStride, networkDraft,
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

test("the adapter line and the account's wall time", () => {
  const a = { name: "RTL8821CU USB adapter", usb_id: "0bda:c811", driver: "rtw88_8821cu", interface: "wlan1",
              bands: ["2.4", "5"], reported_by: "seed" };
  eq(adapterLine(a), "RTL8821CU USB adapter · 0bda:c811 · rtw88_8821cu on wlan1 · 2.4 / 5 GHz");
  eq(adapterLine({ ...a, mac_known: true }), "RTL8821CU USB adapter · 0bda:c811 · MAC on record · rtw88_8821cu on wlan1 · 2.4 / 5 GHz");
  eq(adapterLine({ name: "X", usb_id: null, driver: null, interface: null, bands: [] }), "X · VID:PID not reported yet");
  eq(adapterLine(null), "no adapter recorded");
  eq(adapterSource(a), "recorded by hand - the sensor will report it itself in the live-upload phase");
  eq(adapterSource({ ...a, reported_by: "sensor" }), "reported by the sensor");
  // CEST (+2), CET (+1) after 25 Oct 2026, and back
  eq(zonedToUtc("2026-10-12T18:00", TZ), "2026-10-12T16:00:00Z");
  eq(zonedToUtc("2026-11-02T08:30", TZ), "2026-11-02T07:30:00Z");
  eq(zonedToUtc("2027-03-29T12:00", TZ), "2027-03-29T10:00:00Z");
  eq(zonedToUtc("garbage", TZ), null);
  eq(utcToZoned("2026-10-12T16:00:00Z", TZ), "2026-10-12T18:00");
  eq(utcToZoned("2026-11-02T07:30:00Z", TZ), "2026-11-02T08:30");
});

test("a hardware change: when and an optional note", () => {
  const now = Date.parse("2026-10-13T12:00:00Z");
  eq(hardwareChangeProblem("2026-10-13T09:00:00Z", now), null);                  // in the past: recorded later
  eq(hardwareChangeProblem("2026-10-13T12:03:00Z", now), null);                  // 5 min of clock skew
  eq(hardwareChangeProblem("2026-10-13T13:00:00Z", now), "a change cannot lie in the future");
  eq(hardwareChangeProblem(null, now), "give the date and time of the change");
  eq(hardwareChangeBody("2026-10-13T09:00:00Z", " swapped antenna "), { valid_from: "2026-10-13T09:00:00Z", note: "swapped antenna" });
  eq(hardwareChangeBody("2026-10-13T09:00:00Z", "  "), { valid_from: "2026-10-13T09:00:00Z", note: null });
});

test("history rows", () => {
  const rows = historyRows([
    { sensor: "pi-fri", ts: "2026-10-12T16:00:00Z", boundary: true, kind: "hardware_change", label: "Hardware change: swapped antenna" },
    { sensor: "pi-fri", ts: "2026-09-27T13:04:57Z", boundary: true, kind: "change", label: "Hop list: 89 entries" },
    { sensor: "pi-fri", ts: "2026-09-27T14:15:50Z", boundary: false, kind: "change", label: "Hop guard" }], TZ);
  eq(rows.map((r) => [r.ts, r.effect]), [["12 Oct 18:00", "RSSI baselines start again"], ["27 Sep 15:04", "evaluated apart"],
                                         ["27 Sep 16:15", "no boundary"]]);
});

test("sensors: names, the body, rename and the guarded delete", () => {
  const sensors = [{ name: "pi-fri" }, { name: "pi-lab" }];
  eq(sensorNameProblem("pi-new", sensors), null);
  eq(sensorNameProblem(" pi-fri ", sensors), "a sensor named pi-fri exists already");
  eq(sensorNameProblem("pi-fri", sensors, "pi-fri"), null);                       // its own name when editing
  eq(sensorNameProblem("", sensors), "give the sensor a name");
  eq(sensorNameProblem("-x", sensors), "1 to 40 characters: letters, digits, . _ - (not first)");
  eq(sensorNameProblem("a b", sensors) != null, true);
  eq(sensorBody({ name: " pi-lab ", location: " Lab ", description: "  " }), { name: "pi-lab", location: "Lab", description: null });
  eq(renameNote("pi-fri", "pi-dorm"),
     "Renamed pi-fri to pi-dorm. The importer files data under the name: use --sensor pi-dorm from now on, or the next import creates a new sensor pi-fri.");
  // a sensor with data is never deletable; an empty one needs its name typed
  eq(deleteGuard({ has_polls: true }, "pi-fri", "pi-fri").allowed, false);
  eq(deleteGuard({ has_polls: false }, "pi-new", "").why, "type pi-new to confirm");
  eq(deleteGuard({ has_polls: false }, "pi-new", "pi-ne").allowed, false);
  eq(deleteGuard({ has_polls: false }, "pi-new", " pi-new ").allowed, true);
});

done();
