// The account and the shell's status sources: /api/v2/sensors (account,
// sensors, latest data; every 60 s) and /api/health (every 60 s, SPEC 3).

import { ep } from "../data/keys.js";
import { useQuery } from "../data/query.js";
import { useSensors } from "./useFilters.js";

export const DEFAULT_TZ = "Europe/Bratislava";

export function useAccount() {
  const { data } = useSensors();
  return {
    name: data?.account?.name ?? null,
    tz: data?.account?.tz ?? DEFAULT_TZ,
    sensors: data?.sensors ?? null,
    offlineMin: data?.offline_after_min ?? 15,
    latestData: data?.latest_data ?? null,
  };
}

// {tone: ok|bad, text, schema}
export function useHealth() {
  const [key, url] = ep.health();
  const { data, error } = useQuery(key, url, { refetchMs: 60_000, staleMs: 55_000 });
  if (data && !error) return { tone: "d-ok", text: `API ok · database ${data.database}`, schema: data.schema_version };
  if (error) {
    const dbDown = error.startsWith("503");
    return { tone: "d-bad", text: dbDown ? "API ok · database unavailable" : "API unreachable", schema: null };
  }
  return { tone: "d-neu", text: "checking…", schema: null };
}
