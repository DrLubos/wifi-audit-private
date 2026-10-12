import { test, eq, done } from "../../testing/tiny.js";
import {
  actionsFor, apCell, bssidCell, groupActions, groupText, headText, pagerText, parseStatus, rowOf, selected, severityBadge,
  statusBadge, statusChips, statusParam, timeTitle, typeText, whoRows, whyRows,
} from "./events.js";

const TZ = "Europe/Bratislava";

test("feed cells", () => {
  eq(severityBadge("medium"), { cls: "warn", text: "Medium" });
  eq(statusBadge("false_positive"), { cls: "info", text: "False positive" });
  eq(typeText({ kind: "detection", type_label: "Deauthentication flood" }), "Detection · Deauthentication flood");
  eq(typeText({ kind: "kismet", type: "DEAUTHFLOOD" }), "Kismet · DEAUTHFLOOD");
  eq(typeText({ kind: "config" }), "Configuration change");
  eq(typeText({ kind: "sensor" }), "Sensor");
  eq(apCell({ ap: null }), { text: "–", hidden: false });
  eq(apCell({ ap: { network: null } }), { text: "hidden network", hidden: true });
  eq(apCell({ ap: { network: "IK-WIFI" } }).text, "IK-WIFI");
  eq(timeTitle({ ts: "2026-10-05T05:03:50Z", ts_end: "2026-10-05T05:05:37Z" }, TZ), "5 Oct 07:03 – 07:05");
  eq(timeTitle({ ts: "2026-10-06T19:33:15Z", ts_end: "2026-10-10T21:53:47Z" }, TZ), "6 Oct 21:33 – 10 Oct 23:53");
  eq(timeTitle({ ts: "2026-10-05T05:03:50Z", ts_end: null }, TZ), undefined);
  eq(headText({ open: 271, total: 271 }), "271 open · 271 in total · newest first");
  eq(statusChips({ open: 3, confirmed: 241, false_positive: 68 }).map((c) => c.label),
     ["Open 3", "Confirmed 241", "False positive 68"]);
  eq(pagerText(2, 50, 50, 38, 38), "51–100 of 38 · only the newest are loaded, older ones on request");
  eq(pagerText(1, 50, 50, 159, 271), "1–50 of 159 rows · 271 events, repeated low ones grouped per network and day");
  eq(pagerText(1, 50, 0, 0, 0), "no event matches the filters");
  eq(actionsFor({ status: "open", kind: "kismet" }), ["Confirm", "False positive"]);
  eq(actionsFor({ status: "open", kind: "sensor" }), ["Confirm"]);
  eq(actionsFor({ status: "confirmed", kind: "detection" }), []);
  eq(groupActions({ status: "open", kind: "kismet" }), ["Confirm all", "All false positive"]);
  eq(groupActions({ status: "open", kind: "config" }), ["Confirm all"]);
});

test("status filter: Open by default, all, a list", () => {
  eq(parseStatus(null), ["open"]);
  eq(parseStatus(""), ["open"]);
  eq(parseStatus("all"), []);
  eq(parseStatus("confirmed,false_positive"), ["confirmed", "false_positive"]);
  eq(parseStatus("closed"), ["open"]);
  eq(statusParam(["open"]), null);                     // the default stays out of the URL
  eq(statusParam([]), "all");
  eq(statusParam(["open", "confirmed"]), "open,confirmed");
  const counts = { open: 271, confirmed: 2, false_positive: 3, total: 276 };
  eq(selected(counts, ["open"]), 271);
  eq(selected(counts, ["confirmed", "false_positive"]), 5);
  eq(selected(counts, []), 276);
  eq(selected(undefined, []), 0);
});

test("grouped rows", () => {
  const g = { id: 372, kind: "kismet", ts: "2026-10-11T15:31:15Z",
              group: { n: 3, day: "2026-10-11", ts_first: "2026-10-11T00:17:31Z", ts_last: "2026-10-11T15:31:15Z",
                       members: [{ id: 372 }, { id: 371 }, { id: 370 }] } };
  eq(groupText(g, TZ), "3 episodes on 11 Oct, 02:17 – 17:31");
  // an episode that runs past midnight ends on the next day
  eq(groupText({ ...g, kind: "config", group: { ...g.group, ts_last: "2026-10-11T22:30:00Z" } }, TZ),
     "3 changes on 11 Oct, 02:17 – 12 Oct 00:30");
  const items = [{ id: 400 }, g, { id: 360 }];
  eq(rowOf(items, 371), g);
  eq(rowOf(items, 372), g);
  eq(rowOf(items, 360), items[2]);
  eq(rowOf(items, 1), null);
  eq(rowOf(items, null), null);
  // a network's group: the number of its BSSIDs, the list in the title
  const net = { ap: { bssid: "EC:58:EA:54:45:8C" }, group: { bssids: 2, members: [{ bssid: "EC:58:EA:54:45:8C" }, { bssid: "EC:58:EA:55:8E:38" }] } };
  eq(bssidCell(net), { text: "2 BSSIDs", mono: false, title: "EC:58:EA:54:45:8C, EC:58:EA:55:8E:38" });
  eq(bssidCell({ ap: { bssid: "EC:58:EA:54:45:8C" }, group: { bssids: 1, members: [] } }).text, "EC:58:EA:54:45:8C");
  eq(bssidCell({ ap: null }).text, "–");
});

test("why it fired", () => {
  const rule = whyRows({ kind: "detection", type: "unknown_bssid_managed_ssid", summary: "x",
                         ap: { bssid: "F0:6F:CE:41:1F:B0", network: "IK-WIFI", classification: "managed" },
                         evidence: { channel: null, network: "IK-WIFI", security: "unknown", allowed_ouis: ["EC:58:EA", "3C:46:A1"],
                                     random_bssid: false, allowed_bssids: 0, security_label: "Unknown" } }, TZ);
  eq(rule.map((r) => [r.dt, r.dd]), [
    ["BSSID", "F0:6F:CE:41:1F:B0"], ["Broadcasts SSID", "IK-WIFI (managed network)"],
    ["Allowed at the time", "OUIs EC:58:EA, 3C:46:A1 – this one is not among them"], ["Security", "Unknown"],
    ["Channel", "–"], ["Now", "classified managed - the network's rules have changed since"]]);
  const kis = whyRows({ kind: "kismet", type: "DEAUTHFLOOD",
                        evidence: { last: "2026-10-05T05:05:37.643853+00:00", first: "2026-10-05T05:03:50.52046+00:00", alerts: 3,
                                    header: "DEAUTHFLOOD", channels: ["0"], kismet_severity: 10 } }, TZ);
  eq(kis.map((r) => [r.dt, r.dd]), [["Kismet alert", "DEAUTHFLOOD"], ["Alerts", "3, 5 Oct 07:03 – 07:05"],
                                    ["Kismet severity", "10"]]);
  const cfg = whyRows({ kind: "config", summary: "MFP no → capable",
                        evidence: { to: { mfp: "capable", crypt: "WPA3", label: "WPA2/WPA3 transition", level: "psk_no_mfp" },
                                    from: { mfp: "no", crypt: "WPA3", label: "WPA2/WPA3 transition", level: "psk_no_mfp" } } }, TZ);
  eq(cfg.map((r) => [r.dt, r.dd]), [["MFP", "no → capable"]]);
  const sen = whyRows({ kind: "sensor", ts: "2026-10-06T19:33:15Z", ts_end: "2026-10-10T21:53:47Z",
                        evidence: { kind: "outage", cause: "Capture adapter lost from the USB bus", polls: 11801,
                                    ongoing: false, duration_s: 354032 } }, TZ);
  eq(sen.map((r) => [r.dt, r.dd]), [["Kind", "no poll collected (outage)"], ["Window", "6 Oct 21:33 – 10 Oct 23:53"],
                                    ["Duration", "4 d"], ["Polls in it", "11,801"],
                                    ["Cause", "Capture adapter lost from the USB bus"]]);
  const flood = whyRows({ kind: "detection", type: "deauth_flood", summary: "x",
                          evidence: { ap: { mfp_req: false }, rules: { alerts: false, bursts: true },
                                      bursts: { n: 3, threshold: 3 }, params: { burst_window_s: 600 },
                                      observed: { bursts_per_hour: 28.877, ratio_to_baseline: 162.4 },
                                      baseline: { bursts_per_hour: 0.1779, from: "2026-09-08T00:00:00Z", to: "2026-09-28T00:05:00Z", polls: 29687 },
                                      duration_s: 374 } }, TZ);
  eq(flood.map((r) => [r.dt, r.dd]), [
    ["Rule", "3 deauth/disassoc bursts within 10 min (threshold 3)"],
    ["Rate", "28.9 bursts/h, the AP's baseline 0.2/h (162×)"], ["Baseline", "8 Sep – 28 Sep, 29,687 polls"],
    ["Duration", "6 min"], ["MFP required", "no - clients can be disconnected by forged frames"]]);
});

test("who saw it", () => {
  const w = whoRows({ who_saw_it: [
    { sensor: "pi-fri", seen: true, rssi_median: -66.0, clients: 7, first_seen: "2026-09-15T13:53:10Z", last_seen: "2026-10-11T20:46:36Z" },
    { sensor: "pi-lab", seen: false, rssi_median: null, clients: null, first_seen: "2026-09-27T11:30:00Z", last_seen: "2026-10-10T14:41:00Z" },
    { sensor: "pi-x", seen: true, rssi_median: null, clients: null, first_seen: "2026-09-27T11:30:00Z", last_seen: "2026-10-10T14:41:00Z" }] }, TZ);
  eq(w.map((r) => r.dd), [
    "−66 dBm then · up to 7 clients then · known since 15 Sep, last seen 11 Oct 22:46",
    "not seen in the event's hours · known since 27 Sep, last seen 10 Oct 16:41",
    "only floor readings then · known since 27 Sep, last seen 10 Oct 16:41"]);
});

done();
