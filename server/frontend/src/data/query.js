// useQuery(key, url): one cache entry per key (data/store.js), in-flight
// de-duplication, ETag revalidation once the entry is older than staleMs,
// the previous key's data kept while a new key loads (charts hold their frame
// instead of flashing; isPrevious marks it), per-card error. refetchMs polls
// while the tab is visible (status cards).

import { useEffect, useRef, useSyncExternalStore } from "react";
import { getJson } from "./api.js";
import { createStore } from "./store.js";

export const store = createStore(getJson);

export function useQuery(key, url, { staleMs = 60_000, refetchMs = 0 } = {}) {
  const k = Array.isArray(key) ? key.join("|") : key;
  const entry = useSyncExternalStore(
    (fn) => store.subscribe(k, fn),
    () => store.get(k),
  );

  useEffect(() => {
    if (!url) return undefined;
    if (store.isStale(k, url, staleMs)) store.fetchKey(k, url);
    if (!refetchMs) return undefined;
    const id = setInterval(() => {
      if (!document.hidden) store.fetchKey(k, url);
    }, refetchMs);
    return () => clearInterval(id);
  }, [k, url, staleMs, refetchMs]);

  const last = useRef(undefined);
  if (entry.data !== undefined) last.current = entry.data;
  const data = entry.data !== undefined ? entry.data : last.current;
  return {
    data,
    error: entry.error,
    status: entry.status,
    isFetching: Boolean(entry.inFlight),
    isPrevious: entry.data === undefined && data !== undefined,
  };
}

export const invalidate = (prefix) => store.invalidate(prefix);
