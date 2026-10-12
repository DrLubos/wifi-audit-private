import { atest, eq, done } from "../testing/tiny.js";
import { createWriteState } from "./write.js";

function memory(init = {}) {
  const m = { ...init };
  return { getItem: (k) => m[k] ?? null, setItem: (k, v) => { m[k] = v; }, removeItem: (k) => { delete m[k]; }, m };
}
function respond(status, body) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}
function fakeFetch(...responses) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push([url, init.method, init.headers?.Authorization ?? null, init.body ?? null]);
    return responses.shift();
  };
  fn.calls = calls;
  return fn;
}
function fakeCache() {
  const c = { stale: 0, refetched: [] };
  c.markStale = () => { c.stale += 1; };
  c.invalidate = (p) => { c.refetched.push(p); };
  return c;
}

await atest("unlock keeps the token for the tab, a wrong one does not", async () => {
  const storage = memory();
  const f = fakeFetch(respond(401, { detail: "write token required" }), respond(204));
  const w = createWriteState(storage, f, null);
  eq(w.get().unlocked, false);
  let err = null;
  await w.unlock("nope").catch((e) => { err = e; });
  eq([err.status, err.message, w.get().unlocked], [401, "wrong write token", false]);
  await w.unlock(" secret-token ");
  eq([w.get().unlocked, storage.m["wifi-audit.write-token"]], [true, "secret-token"]);
  eq(f.calls[1], ["/api/v2/auth/check", "POST", "Bearer secret-token", null]);
  // a new page of the same tab starts unlocked
  eq(createWriteState(storage, f, null).get().unlocked, true);
});

await atest("writes disabled on the server", async () => {
  const w = createWriteState(memory(), fakeFetch(respond(403, {})), null);
  let err = null;
  await w.unlock("x").catch((e) => { err = e; });
  eq(err.status, 403);
});

await atest("send: the token, the body, stale reads, refetched keys; 401 locks", async () => {
  const cache = fakeCache();
  const f = fakeFetch(respond(200, { ok: 1 }), respond(422, { detail: "ssid: 1 to 32 bytes" }), respond(401, {}));
  const w = createWriteState(memory({ "wifi-audit.write-token": "t" }), f, cache);
  eq(await w.send("PUT", "/api/v2/settings/networks", { networks: [] }, ["settings.networks"]), { ok: 1 });
  eq(f.calls[0], ["/api/v2/settings/networks", "PUT", "Bearer t", '{"networks":[]}']);
  eq([cache.stale, cache.refetched], [1, ["settings.networks"]]);
  let err = null;
  await w.send("PUT", "/x", {}).catch((e) => { err = e; });
  eq([err.status, err.message, w.get().unlocked], [422, "ssid: 1 to 32 bytes", true]);
  await w.send("PUT", "/x", {}).catch((e) => { err = e; });
  eq([err.status, w.get().unlocked], [401, false]);
  eq(cache.stale, 1);                                   // failed writes leave the cache alone
});

await atest("locked: no request", async () => {
  const f = fakeFetch();
  const w = createWriteState(memory(), f, null);
  let err = null;
  await w.send("PUT", "/x", {}).catch((e) => { err = e; });
  eq([err.status, f.calls.length], [401, 0]);
});

done();
