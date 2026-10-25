import { test, eq, done } from "../../testing/tiny.js";
import {
  adapterText, apsText, bufferText, configText, coverageColumns, coverageText, crashText, dayNotes, durText, footText, headText,
  hopBadge, lastPollText, legendItems, liveText, localDate, outageRows, restartText, sinceText, stateNote, uploadText,
} from "./sensors.js";
import { sensorState } from "../../app/status.js";

const TZ = "Europe/Bratislava";
const NOW = Date.parse("2026-10-12T16:20:00Z");

// /api/v2/sensors on 2026-10-12 (pi-fri, between imports)
const FRI = {
  name: "pi-fri", location: "internat-df-3-poschodie", hardware: "Raspberry Pi 3B+ · RTL8821CU (USB)",
  status: "offline", first_poll: "2026-09-15T13:53:07Z", last_poll: "2026-10-11T20:46:41Z",
  last_capture_at: "2026-10-11T20:46:41Z", hop: { n: 89, visited: 89, ok: true }, poll_s: 30.0,
  last_upload: { at: "2026-10-11T20:50:10Z", via: "snapshot", buffer_bytes: 603585312, buffer_version: "7",
                 polls_from: "2026-09-27T20:16:30Z", polls_to: "2026-10-11T20:46:41Z" },
  coverage_7d: 0.408, window_7d: { from: "2026-10-04T21:00:00Z", to: "2026-10-11T20:46:41Z" }, outage_since: null,
  crashes_24h: { count: 3, within_60s: 2, max_recovery_s: 353978, restarts: 3 },
  aps_7d: 316, aps_only_here_7d: 316,
  config: { version: 3, plan: "all_89", n_channels: 89, dwell_s: 0.2, poll_s: 30, confirmed_at: "2026-09-27T13:04:57Z" },
  pending: null,
};

test("durations and the head", () => {
  eq([durText(7), durText(335), durText(19500), durText(3654), durText(354032), durText(86400)],
     ["7 s", "5 min", "5 h 25 min", "1 h", "4 d 2 h", "1 d"]);
  eq(headText([FRI], 15, NOW, TZ), "1 sensor in the account · 0 online · 1 between imports");
  const live = { ...FRI, last_poll: "2026-10-12T16:19:50Z", last_capture_at: "2026-10-12T16:19:50Z" };
  const down = { ...FRI, name: "pi-lab", last_capture_at: "2026-10-10T12:00:00Z" };
  eq(headText([live, down], 15, NOW, TZ), "2 sensors in the account · 1 online · 1 offline");
});

test("the card", () => {
  eq(lastPollText(FRI, NOW, TZ), "11 Oct 22:46 (every 30 s)");
  eq(lastPollText({ ...FRI, last_poll: "2026-10-12T16:19:48Z" }, NOW, TZ), "12 s ago (every 30 s)");
  eq(uploadText(FRI.last_upload, NOW, TZ), "11 Oct 22:50 · snapshot import, data to 22:46");
  eq(uploadText({ at: "2026-10-12T16:19:48Z", via: "ingest" }, NOW, TZ), "12 s ago · after every poll");
  eq(uploadText(null, NOW, TZ), "none yet");
  eq(bufferText(FRI.last_upload), "604 MB · 14 days");
  eq(bufferText({ buffer_bytes: 152e6 }), "152 MB");
  eq(coverageText(FRI), "40.8 % of the time collecting");
  // the end of the 4-day outage counts as a crash recovery; restarts are not crashes
  eq(crashText(FRI.crashes_24h), "3 (2 within 1 min, the longest after 4 d 2 h)");
  eq(crashText({ count: 2, within_60s: 2, max_recovery_s: 7 }), "2 (each recovered within 7 s)");
  eq(crashText({ count: 0 }), "none");
  eq(restartText(FRI.crashes_24h), "3 restarts without a crash");
  eq(restartText({ restarts: 0 }), null);
  eq(apsText(FRI, 1), "316");
  eq(apsText({ aps_7d: 648, aps_only_here_7d: 241 }, 2), "648 · 241 of them only by this sensor");
  eq(configText(FRI.config), "89 channels · 0.2 s dwell · poll 30 s");
  eq(configText({ plan: "ht20_37", n_channels: 37, dwell_s: 0.2, poll_s: 30 }), "37 channels (20 MHz only) · 0.2 s dwell · poll 30 s");
  eq(hopBadge(FRI), { cls: "ok", text: "Channels 89/89", title: "hop entries visited / in the list, at the last collecting poll" });
  eq(hopBadge({ hop: { n: 90, visited: 45, ok: true } }).cls, "warn");
  eq(footText(FRI, TZ), { warn: false, text: "Configuration in effect since 27 Sep 15:04" });
  const p60 = { version: 4, plan: "all_89", n_channels: 89, dwell_s: 0.2, poll_s: 60 };
  eq(footText({ ...FRI, pending: p60 }, TZ).text,
     "Pending change: 89 channels · 0.2 s dwell · poll 60 s - this sensor does not accept configuration (CONFIG_APPLY=0)");
  eq(footText({ ...FRI, pending: p60, config_delivery: { accepting: true, attempt: null } }, TZ).text,
     "Pending change: 89 channels · 0.2 s dwell · poll 60 s - waiting for the sensor's next upload");
  eq(footText({ ...FRI, pending: { ...p60, refused_at: "2026-10-25T10:05:00Z", refused_reason: "rejected: 14 not supported" },
                config_delivery: { accepting: true } }, TZ),
     { warn: true, text: "Refused change: 89 channels · 0.2 s dwell · poll 60 s - rejected: 14 not supported" });
  eq(sinceText(FRI, TZ), "since 15 Sep 2026");
  const ad = { name: "RTL8821CU USB adapter", usb_id: "0bda:c811" };
  eq(adapterText(ad, "2026-10-13T12:00:00Z", TZ), "RTL8821CU USB adapter · 0bda:c811 · last hardware change 13 Oct 14:00");
  eq(adapterText(ad, null, TZ), "RTL8821CU USB adapter · 0bda:c811 · no hardware change recorded");
  eq(adapterText({ name: "RTL8821CU USB adapter", usb_id: null }, null, TZ),
     "RTL8821CU USB adapter · VID:PID not reported yet · no hardware change recorded");
  eq(adapterText(null, null, TZ), "not recorded");
  eq(stateNote(FRI, { tone: "neu" }, TZ).text.slice(0, 44), "The data ends at the last import (11 Oct 22:");
  eq(stateNote({ ...FRI, outage_since: "2026-10-06T19:33:15Z" }, { offline: true }, TZ).text.slice(0, 40),
     "Not collecting since 6 Oct 21:33. Check ");
  eq(stateNote(FRI, { tone: "ok" }, TZ), null);
});

// live upload (schema 14)
const LIVE = { ...FRI, status: "online", last_poll: "2026-10-12T16:19:50Z", last_capture_at: "2026-10-12T16:19:50Z",
  contact: { last_contact_at: "2026-10-12T16:19:52Z", last_batch_at: "2026-10-12T16:19:52Z", backlog_polls: 0,
             held_polls: 0, backlog_oldest: null, clock_offset_s: 0.2, unverified_now: 0,
             missing_polls: { lost: 0, unverified: 0 }, refresh_pending_since: null } };

test("live upload", () => {
  eq(liveText(LIVE, NOW), "last contact 8 s ago · clock +0.2 s · 0 polls lost · 0 time-unverified");
  eq(liveText(FRI, NOW), null);
  eq(stateNote(LIVE, sensorState(LIVE, 15, NOW, TZ), TZ), null);
  const up = { ...LIVE, status: "catching_up",
               contact: { ...LIVE.contact, backlog_polls: 9963, backlog_oldest: "2026-10-14T05:21:58Z" } };
  eq(stateNote(up, sensorState(up, 15, NOW, TZ), TZ).text, "Uploading the backlog: 9,963 polls since 14 Oct 07:21, oldest first.");
  const gone = { ...LIVE, status: "unreachable", contact: { ...LIVE.contact, last_contact_at: "2026-10-12T13:00:00Z" } };
  const note = stateNote(gone, sensorState(gone, 15, NOW, TZ), TZ);
  eq([note.bad, note.text.slice(0, 35)], [true, "No contact since 12 Oct 15:00. The "]);
  eq(headText([LIVE, up], 15, NOW, TZ), "2 sensors in the account · 1 online · 1 catching up");
});

const OUTAGES = [
  { type: "outage", from: "2026-10-06T19:33:15Z", to: "2026-10-10T21:53:47Z", ongoing: false, duration_s: 354032,
    polls: 11801, source: "polls", cause: "Capture adapter lost from the USB bus" },
  { type: "outage", from: "2026-10-06T12:23:51Z", to: "2026-10-06T13:24:45Z", ongoing: false, duration_s: 3654,
    polls: 0, source: "polls", cause: "No polls for 1 h, collector restarted 13:24:44 (not investigated)" },
  { type: "limited", from: "2026-09-26T22:45:26Z", to: "2026-09-27T13:04:57Z", ongoing: false, duration_s: 51571,
    polls: null, source: "annotation", cause: "45 of 90 hop entries visited (stride 4 vs 90)" },
  { type: "outage", from: "2026-09-27T12:18:24Z", to: "2026-09-27T12:23:24Z", ongoing: false, duration_s: 300,
    polls: 10, source: "polls", cause: null },
];

test("coverage columns and the outage table", () => {
  eq(localDate("2026-10-06T22:30:00Z", TZ), "2026-10-07");
  eq(dayNotes("2026-10-06", OUTAGES, TZ), ["Capture adapter lost from the USB bus",
                                         "No polls for 1 h, collector restarted 13:24:44 (not investigated)"]);
  eq(dayNotes("2026-10-09", OUTAGES, TZ), ["Capture adapter lost from the USB bus"]);
  eq(dayNotes("2026-10-11", OUTAGES, TZ), []);
  const days = [
    { date: "2026-10-06", ok: 0.855, limited: 0, outage: 0.145, not_installed: 0, no_data: 0 },
    { date: "2026-10-11", ok: 0.949, limited: 0, outage: 0, not_installed: 0, no_data: 0.051 },
    { date: "2026-10-12", ok: 0, limited: 0, outage: 0, not_installed: 0, no_data: 1 },
  ];
  const cols = coverageColumns(days, "pi-fri", OUTAGES, TZ, "2026-10-12");
  eq(cols.map((c) => c.label), ["6 Oct", "11 Oct", "12 Oct"]);
  eq(cols[0].head, "pi-fri · Tue 6 Oct");
  eq(cols[2].head, "pi-fri · Mon 12 Oct (today)");
  eq(cols[0].segs.map((s) => [s.key, s.share]), [["outage", 0.145], ["ok", 0.855]]);    // top to bottom
  eq(cols[1].rows.map((r) => `${r.v} ${r.l}`), ["5.1 % no data yet (after the last import)",
                                                "94.9 % successful polls, full channel list"]);
  eq(cols[2].rows.map((r) => r.v), ["100.0 %", "0.0 %"]);
  eq(cols[0].aria, "Tue 6 Oct: 14.5 % outage, 85.5 % successful polls, full channel list");
  eq(legendItems(days).map((c) => c.key), ["ok", "outage", "no_data"]);
  const rows = outageRows(OUTAGES, TZ);
  eq(rows[0].from, "6 Oct 21:33");
  eq(rows[0].duration, "4 d 2 h");
  eq(rows[0].type, { cls: "bad", text: "Outage" });
  eq(rows[2].type.text, "Limited channels");
  eq(rows[2].documented, true);
  eq(rows[3].cause, "not documented");
  eq(outageRows([{ ...OUTAGES[0], to: null, ongoing: true }], TZ)[0].duration, "4 d 2 h (ongoing)");
});

done();
