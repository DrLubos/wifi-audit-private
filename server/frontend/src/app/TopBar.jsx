// The top bar (SPEC section 3): the filters the page uses (route handle), the
// time of the newest stored poll, the write state (locked: a link to the
// token box in Settings; unlocked: Lock).

import { Link } from "react-router-dom";
import { useWrite } from "../data/write.js";
import { RANGES } from "../hooks/filters.js";
import { useAccount } from "../hooks/useAccount.js";
import { useFilters } from "../hooks/useFilters.js";
import { fmtDateTime } from "../format.js";

const RANGE_LABEL = { "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days" };

export default function TopBar() {
  const { keys, sensor, range, setFilter } = useFilters();
  const account = useAccount();
  const w = useWrite();
  const names = (account.sensors ?? []).map((s) => s.name);
  return (
    <header className="top v2">
      <div className="filters">
        {keys.includes("sensor") && (
          <label className="fl">Sensor
            <select className="sel" value={sensor} onChange={(e) => setFilter("sensor", e.target.value)}>
              <option value="all">All sensors ({names.length})</option>
              {names.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        )}
        {keys.includes("range") && (
          <label className="fl">Period
            <select className="sel" value={range} onChange={(e) => setFilter("range", e.target.value)}>
              {RANGES.map((r) => <option key={r} value={r}>{RANGE_LABEL[r]}</option>)}
            </select>
          </label>
        )}
      </div>
      <div className="sync">
        <span>Latest data: {account.latestData ? fmtDateTime(account.latestData, account.tz) : "–"}</span>
        {w.unlocked ? (
          <>
            <span className="b ok">Write unlocked</span>
            <button type="button" className="btn2 lock-btn" onClick={w.lock}>Lock</button>
          </>
        ) : (
          <Link to="/settings/networks" className="b neu" title="Unlock with the write token in Settings">Write locked</Link>
        )}
      </div>
    </header>
  );
}
