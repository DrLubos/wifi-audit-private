// The global filters of the current route (SPEC section 3, PLAN.md 8.2):
// which keys a page uses comes from its route handle ({filters: [...]});
// values from the URL, else the last choice, else the defaults; the URL is
// completed with replace; setFilter pushes a history entry and remembers the
// choice; linkSearch() is the ?sensor=…&range=… the sidebar links carry.

import { useCallback, useEffect } from "react";
import { useMatches, useSearchParams } from "react-router-dom";
import { ep } from "../data/keys.js";
import { useQuery } from "../data/query.js";
import { filterSearch, readStored, resolveFilters, writeStored } from "./filters.js";

function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function useRouteFilters() {
  const matches = useMatches();
  for (let i = matches.length - 1; i >= 0; i -= 1) {
    const f = matches[i].handle?.filters;
    if (f) return f;
  }
  return [];
}

export function useSensors() {
  const [key, url] = ep.sensors();
  return useQuery(key, url, { refetchMs: 60_000 });
}

export function useFilters() {
  const keys = useRouteFilters();
  const [params, setParams] = useSearchParams();
  const { data } = useSensors();
  const names = data ? data.sensors.map((s) => s.name) : null;
  const stored = readStored(storage());
  const get = (k) => params.get(k);
  const { values, changed } = resolveFilters(keys, get, stored, names);
  const all = resolveFilters(["sensor", "range"], get, stored, names).values;
  const keyList = keys.join(",");

  useEffect(() => {
    if (!changed) return;
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const k of keyList.split(",").filter(Boolean)) next.set(k, values[k]);
      return next;
    }, { replace: true });
  }, [changed, keyList, values.sensor, values.range, setParams]);

  const setFilter = useCallback((k, v) => {
    writeStored(storage(), { ...all, [k]: v });
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set(k, v);
      return next;
    });
  }, [all.sensor, all.range, setParams]);

  return {
    keys,
    sensor: values.sensor,
    range: values.range,
    setFilter,
    linkSearch: () => filterSearch(all),
  };
}
