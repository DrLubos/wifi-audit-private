// The rogue banner (Networks board): unknown BSSIDs that broadcast a managed
// SSID (classification suspicious), with the open event; "Add to allowed
// BSSIDs" needs the write token (phase 5).

import { Link } from "react-router-dom";
import { fmtDateTime, fmtDbm } from "../../format.js";

const SHOWN = 3;

function sensorsText(rssi) {
  const e = Object.entries(rssi ?? {});
  if (e.length === 1) return `${e[0][0]} only${e[0][1] == null ? "" : `, ${fmtDbm(e[0][1])} dBm`}`;
  return e.map(([k, v]) => (v == null ? k : `${k} ${fmtDbm(v)} dBm`)).join(", ");
}

function line(r, tz) {
  const security = r.label === "Unknown" ? "security unknown" : r.reason ? `${r.label} (${r.reason})` : r.label;
  return [security, r.random_bssid ? "randomized BSSID" : null, sensorsText(r.rssi), `since ${fmtDateTime(r.since, tz)}`]
    .filter(Boolean).join(" · ");
}

export default function Rogue({ rogue, tz }) {
  if (!rogue?.length) return null;
  const first = rogue.find((r) => r.event_id);
  return (
    <section className="rogue" aria-labelledby="rogue-h">
      <div className="text">
        <h2 id="rogue-h">
          {rogue.length === 1 ? "1 unknown BSSID is broadcasting a managed SSID"
            : `${rogue.length} unknown BSSIDs are broadcasting a managed SSID`}
        </h2>
        {rogue.slice(0, SHOWN).map((r) => (
          <span className="line" key={r.bssid}>
            <b>{r.ssid}</b> from <span className="mono">{r.bssid}</span> · {line(r, tz)}
          </span>
        ))}
        {rogue.length > SHOWN && <span className="line">+ {rogue.length - SHOWN} more</span>}
      </div>
      <div className="actions">
        {first && <Link to={`/events?id=${first.event_id}`} className="btn">Open event</Link>}
        <button type="button" className="btn2" disabled title="Write actions need the write token (dashboard phase 5)">
          Add to allowed BSSIDs
        </button>
      </div>
    </section>
  );
}
