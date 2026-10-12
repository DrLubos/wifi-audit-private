// Recent events: always the 5 newest rows of the events feed for the selected
// sensors, independent of the period (Main board); a row of repeated low
// events (api: item.group) shows their number.

import { Link } from "react-router-dom";
import { SeverityBadge, StatusBadge } from "../../components/Badge.jsx";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { fmtDateTime, fmtInt } from "../../format.js";

export default function RecentEvents({ sensor, account }) {
  const [key, url] = ep.recentEvents(sensor);
  const q = useQuery(key, url);
  return (
    <section className="card panel events" aria-labelledby="ev-h">
      <div className="card-head">
        <div className="card-title">
          <h3 id="ev-h">Recent events</h3>
          <span className="muted tiny">always the 5 newest, independent of the period</span>
        </div>
        <Link to="/events" className="small">All events</Link>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!q.data && !q.error && <Skeleton height={160} />}
      {q.data && (
        <div className="scroll-x">
          <table className="t nw">
            <thead>
              <tr><th>Time</th><th>Severity</th><th>Event</th><th>AP / network</th><th>Sensor</th><th>Status</th></tr>
            </thead>
            <tbody>
              {q.data.items.map((e) => (
                <tr key={e.id}>
                  <td>{fmtDateTime(e.ts, account.tz)}</td>
                  <td><SeverityBadge severity={e.severity} /></td>
                  <td>{e.summary}{e.group && <span className="muted"> · {fmtInt(e.group.n)}× that day</span>}</td>
                  <td>{e.ap ? (e.ap.network ?? e.ap.ssid ?? <span className="mono">{e.ap.bssid}</span>) : "–"}</td>
                  <td>{e.sensors.join(", ")}</td>
                  <td><StatusBadge status={e.status} /></td>
                </tr>
              ))}
              {q.data.items.length === 0 && (
                <tr><td colSpan={6} className="muted">No events.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
