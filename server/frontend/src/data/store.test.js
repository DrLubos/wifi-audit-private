import { atest, test, eq, done } from "../testing/tiny.js";
import { createStore, EMPTY } from "./store.js";

function fakeApi(responses) {
  const calls = [];
  const getJson = (url, etag) => {
    calls.push([url, etag]);
    const r = responses.shift();
    return r instanceof Error ? Promise.reject(r) : Promise.resolve(r);
  };
  return { calls, getJson };
}

test("an unknown key is the frozen empty entry", () => {
  const s = createStore(() => Promise.resolve({}));
  eq(s.get("x") === EMPTY, true);
});

await atest("load, revalidate with the ETag, keep the data on 304", async () => {
  const api = fakeApi([{ status: 200, data: { a: 1 }, etag: 'W/"1"' }, { status: 304 }]);
  const s = createStore(api.getJson);
  let notified = 0;
  s.subscribe("k", () => { notified += 1; });
  await s.fetchKey("k", "/api/x");
  eq(s.get("k").data, { a: 1 });
  eq(s.get("k").status, "success");
  const before = s.get("k");
  await s.fetchKey("k", "/api/x");
  eq(api.calls, [["/api/x", null], ["/api/x", 'W/"1"']]);
  eq(s.get("k").data === before.data, true);
  eq(notified >= 4, true);
});

await atest("one request per key at a time", async () => {
  const api = fakeApi([{ status: 200, data: 1, etag: null }]);
  const s = createStore(api.getJson);
  const a = s.fetchKey("k", "/u");
  const b = s.fetchKey("k", "/u");
  eq(a === b, true);
  await a;
  eq(api.calls.length, 1);
});

await atest("errors are kept per key, the old data stays", async () => {
  const api = fakeApi([{ status: 200, data: "ok", etag: null }, new Error("HTTP 503")]);
  const s = createStore(api.getJson);
  await s.fetchKey("k", "/u");
  await s.fetchKey("k", "/u");
  eq([s.get("k").status, s.get("k").error, s.get("k").data], ["error", "HTTP 503", "ok"]);
  eq(s.isStale("k", "/u", 60000), true);
});

await atest("stale after the interval or for another url; invalidate refetches a prefix", async () => {
  const api = fakeApi([{ status: 200, data: 1, etag: "a" }, { status: 200, data: 2, etag: "b" }]);
  const s = createStore(api.getJson);
  await s.fetchKey("overview.period|all|7d", "/p");
  eq(s.isStale("overview.period|all|7d", "/p", 60000), false);
  eq(s.isStale("overview.period|all|7d", "/q", 60000), true);
  await s.invalidate("overview.");
  eq(s.get("overview.period|all|7d").data, 2);
});

await atest("a write marks every entry stale without a request", async () => {
  const api = fakeApi([{ status: 200, data: 1, etag: 'W/"1"' }]);
  const s = createStore(api.getJson);
  await s.fetchKey("k", "/u");
  eq(s.isStale("k", "/u", 60_000), false);
  s.markStale();
  eq(api.calls.length, 1);
  eq(s.isStale("k", "/u", 60_000), true);
  eq(s.get("k").data, 1);
});

done();
