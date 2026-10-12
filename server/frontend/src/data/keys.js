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
  networks: (sensor, range, page) => [["networks", sensor, range, page],
    `/api/v2/networks${qs({ sensor, range, other_page: page > 1 ? page : null })}`],
  networksTimeseries: (sensor, range) => [["networks.timeseries", sensor, range],
    `/api/v2/networks/timeseries${qs({ sensor, range })}`],
  clientsHeatmap: (sensor, range, view) => [["clients.heatmap", sensor, range, view],
    `/api/v2/clients/heatmap${qs({ sensor, range, view })}`],
  spectrum: (sensor, range, band) => [["spectrum", sensor, range, band],
    `/api/v2/spectrum${qs({ sensor, range, band })}`],
  spectrumUtilization: (sensor, range, band, view) => [["spectrum.utilization", sensor, range, band, view],
    `/api/v2/spectrum/utilization${qs({ sensor, range, band, view })}`],
  // the list: f = the page's filters (q, cls, band, open, new, hotspots shown, group, page)
  aps: (sensor, range, f, pageSize = 50) => {
    const p = { sensor, range, q: f.q || null, class: f.cls, band: f.band, open: f.open ? "true" : null,
                new: f.new ? "true" : null, hide_hotspots: f.hotspots ? "false" : null,
                group: f.group !== "none" ? f.group : null, page: f.page > 1 ? f.page : null, page_size: pageSize };
    return [["aps", sensor, range, f.q, f.cls, f.band, f.open, f.new, f.hotspots, f.group, f.page], `/api/v2/aps${qs(p)}`];
  },
  ap: (bssid) => [["ap", bssid], `/api/v2/aps/${bssid}`],
  // the feed: f = the page's filters (status, type, managed, q, before / after)
  events: (sensor, f, limit = 50) => {
    const p = { sensor, status: f.status.join(",") || null, type: f.type.join(",") || null,
                managed: f.managed ? "true" : null, q: f.q || null, before: f.before, after: f.after, limit };
    return [["events", sensor, p.status, p.type, f.managed, f.q, f.before, f.after, limit], `/api/v2/events${qs(p)}`];
  },
  event: (id) => [["event", id], `/api/v2/events/${id}`],
  apRssi: (bssid, days = 7) => [["ap.rssi", bssid, days], `/api/v2/aps/${bssid}/rssi${qs({ days })}`],
  apLoad: (bssid, view) => [["ap.load", bssid, view],
    `/api/v2/aps/${bssid}/load${qs(view === "util" ? { view, range: "7d" } : { view, weeks: 4 })}`],
};
