// The v2 endpoints a page reads: [cache key, url]. Keys carry the sensor
// scope, so a later "data changed for sensor X" maps to the keys to refetch.

function qs(params) {
  const parts = Object.entries(params)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export const ep = {
  health: () => ["health", "/api/health"],
  sensors: () => ["sensors", "/api/v2/sensors"],
  overviewStatus: (sensor) => [["overview.status", sensor], `/api/v2/overview/status${qs({ sensor })}`],
  overviewPeriod: (sensor, range) => [["overview.period", sensor, range], `/api/v2/overview/period${qs({ sensor, range })}`],
  recentEvents: (sensor, limit = 5) => [["events.recent", sensor, limit], `/api/v2/events${qs({ sensor, limit })}`],
};
