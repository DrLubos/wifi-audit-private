// The query cache behind useQuery (data/query.js), without React: one entry
// per key {status, data, error, etag, fetchedAt, inFlight, url}, in-flight
// de-duplication, ETag revalidation (a 304 keeps the data), listeners per
// key. Entries are replaced, never mutated, so a snapshot is stable until it
// changes (useSyncExternalStore). Tested in store.test.js.

export const EMPTY = Object.freeze({
  status: "idle", data: undefined, error: null, etag: null, fetchedAt: 0, inFlight: null, url: null,
});

export function createStore(getJson) {
  const entries = new Map();
  const listeners = new Map();

  function get(key) {
    return entries.get(key) ?? EMPTY;
  }

  function set(key, patch) {
    entries.set(key, Object.freeze({ ...get(key), ...patch }));
    for (const fn of listeners.get(key) ?? []) fn();
  }

  function subscribe(key, fn) {
    if (!listeners.has(key)) listeners.set(key, new Set());
    listeners.get(key).add(fn);
    return () => listeners.get(key)?.delete(fn);
  }

  // load (or revalidate) key from url; one request per key at a time
  function fetchKey(key, url) {
    const e = get(key);
    if (e.inFlight && e.url === url) return e.inFlight;
    const p = getJson(url, e.url === url ? e.etag : null).then(
      (r) => {
        if (get(key).inFlight !== p) return;            // a newer request took over
        if (r.status === 304) set(key, { status: "success", fetchedAt: Date.now(), inFlight: null, error: null });
        else set(key, { status: "success", data: r.data, etag: r.etag, error: null, fetchedAt: Date.now(), inFlight: null });
      },
      (err) => {
        if (get(key).inFlight !== p) return;
        set(key, { status: "error", error: err?.message || String(err), inFlight: null, fetchedAt: Date.now() });
      },
    );
    set(key, { inFlight: p, url, status: e.data !== undefined && e.url === url ? "success" : "loading" });
    return p;
  }

  // the entry is older than staleMs (or failed, or never loaded)
  function isStale(key, url, staleMs) {
    const e = get(key);
    if (e.inFlight) return false;
    return e.url !== url || e.status !== "success" || Date.now() - e.fetchedAt > staleMs;
  }

  // refetch every entry whose key starts with prefix (after a write)
  function invalidate(prefix) {
    const done = [];
    for (const [key, e] of entries) {
      if (key.startsWith(prefix) && e.url) done.push(fetchKey(key, e.url));
    }
    return Promise.all(done);
  }

  // after a write: every entry is stale, so a page that mounts later
  // revalidates (no request now; the writer refetches what the page shows)
  function markStale() {
    for (const [key, e] of entries) entries.set(key, Object.freeze({ ...e, fetchedAt: 0 }));
  }

  return { get, subscribe, fetchKey, isStale, invalidate, markStale, entries };
}
