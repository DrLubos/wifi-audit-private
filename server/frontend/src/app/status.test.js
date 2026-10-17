import { test, eq, done } from "../testing/tiny.js";
import { sensorState, severitySummary } from "./status.js";

const TZ = "Europe/Bratislava";
const NOW = "2026-10-11T20:00:00Z";

test("online within the threshold", () => {
  const s = { last_poll: "2026-10-11T19:59:30Z", last_capture_at: "2026-10-11T19:59:30Z" };
  eq(sensorState(s, 15, NOW, TZ).short, "online");
});

test("between imports: data until, not offline", () => {
  const s = { last_poll: "2026-10-10T21:03:47Z", last_capture_at: "2026-10-10T21:03:47Z" };
  const st = sensorState(s, 15, NOW, TZ);
  eq([st.tone, st.short, st.badge], ["neu", "data until 10 Oct 23:03", "Data until 10 Oct 23:03"]);
});

test("the data ends in an outage: offline since the last capture", () => {
  const s = { last_poll: "2026-10-10T21:03:47Z", last_capture_at: "2026-10-06T19:32:45Z" };
  const st = sensorState(s, 15, NOW, TZ);
  eq([st.tone, st.short, st.badge, st.offline], ["bad", "offline 5 d", "Offline 5 d", true]);
});

test("no polls at all", () => {
  eq(sensorState({ last_poll: null }, 15, NOW, TZ).badge, "No data");
});

// live upload (schema 14): the api's status from the contact
const LIVE = { last_poll: "2026-10-11T19:59:45Z", last_capture_at: "2026-10-11T19:59:45Z",
               contact: { last_contact_at: "2026-10-11T19:59:50Z", backlog_polls: 0, held_polls: 0 } };

test("live: online", () => {
  const st = sensorState({ ...LIVE, status: "online" }, 15, NOW, TZ);
  eq([st.tone, st.short, st.live], ["ok", "online", true]);
});

test("live: catching up, clock unverified", () => {
  const up = sensorState({ ...LIVE, status: "catching_up", contact: { ...LIVE.contact, backlog_polls: 4210 } }, 15, NOW, TZ);
  eq([up.tone, up.dot, up.badge], ["warn", "d-warn", "Catching up · 4210 polls"]);
  eq(sensorState({ ...LIVE, status: "clock_unverified" }, 15, NOW, TZ).short, "clock unverified");
});

test("live: unreachable counts from the last contact, not the data", () => {
  const s = { ...LIVE, status: "unreachable", contact: { ...LIVE.contact, last_contact_at: "2026-10-11T17:00:00Z" } };
  const st = sensorState(s, 15, NOW, TZ);
  eq([st.tone, st.badge, st.offline], ["bad", "Unreachable · last seen 3 h ago", true]);
});

test("live: not capturing", () => {
  const s = { ...LIVE, status: "not_capturing", last_capture_at: "2026-10-11T19:00:00Z" };
  eq(sensorState(s, 15, NOW, TZ).short, "not capturing 1 h");
});

test("severity summary", () => {
  eq(severitySummary({ critical: 0, high: 7, medium: 30, low: 221, info: 3 }), "7 high · 30 medium · 221 low · 3 info");
  eq(severitySummary({}), "");
});

done();
