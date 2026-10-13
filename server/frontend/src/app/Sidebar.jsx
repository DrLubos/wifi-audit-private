// The sidebar (SPEC section 3, Main board): brand and account, navigation
// (every link keeps ?sensor=…&range=…), the sensors with a status dot, the
// system status from /api/health (re-checked every 60 s).

import { NavLink } from "react-router-dom";
import { useAccount, useHealth } from "../hooks/useAccount.js";
import { useFilters } from "../hooks/useFilters.js";
import { useNow } from "../hooks/useNow.js";
import { sensorState } from "./status.js";

export const NAV = [
  { label: "Overview", to: "/", end: true },
  { label: "Events", to: "/events" },
  { label: "Networks", to: "/networks" },
  { label: "Access points", to: "/aps" },
  { label: "Spectrum", to: "/spectrum" },
  { label: "Sensors", to: "/sensors" },
  { label: "Settings", to: "/settings" },
];

export default function Sidebar() {
  const account = useAccount();
  const health = useHealth();
  const { linkSearch } = useFilters();
  const now = useNow(30_000);
  const search = linkSearch();
  return (
    <nav className="side" aria-label="Main navigation">
      <div className="brand">
        <b>Wi-Fi Sensor</b>
        <span>Account: {account.name ?? "…"}</span>
      </div>
      <div className="nav">
        {NAV.map((item) => (
          <NavLink key={item.to} to={{ pathname: item.to, search }} end={item.end}>{item.label}</NavLink>
        ))}
      </div>
      <div className="sb first">
        <span className="sbh">Sensors</span>
        {(account.sensors ?? []).map((s) => {
          const st = sensorState(s, account.offlineMin, now, account.tz);
          return (
            <span className="sbi" key={s.name}>
              <span className={`dot ${st.dot}`} />{s.name} · {st.short}
            </span>
          );
        })}
      </div>
      <div className="sb">
        <span className="sbh">System</span>
        <span className="sbi" title="GET /api/health"><span className={`dot ${health.tone}`} />{health.text}</span>
        {health.schema && <span className="sbi indent">schema v{health.schema}</span>}
      </div>
    </nav>
  );
}
