// Overview (/, boards/Main.dc.html): current status (now, sensor filter),
// the selected period (sensor + range), recent events. Every card loads on
// its own (/api/v2/sensors, overview/status, overview/period, events).

import { useAccount } from "../../hooks/useAccount.js";
import { useFilters } from "../../hooks/useFilters.js";
import { useNow } from "../../hooks/useNow.js";
import CurrentStatus from "./CurrentStatus.jsx";
import PeriodSection from "./PeriodSection.jsx";
import RecentEvents from "./RecentEvents.jsx";
import "./overview.css";

export default function Overview() {
  const { sensor, range } = useFilters();
  const account = useAccount();
  const now = useNow(30_000);
  return (
    <main className="content v2">
      <h1>Overview</h1>
      <div className="sec">
        <h2>Current status</h2>
        <span className="muted small">does not depend on the selected period</span>
      </div>
      <CurrentStatus sensor={sensor} account={account} now={now} />
      <PeriodSection sensor={sensor} range={range} account={account} now={now} />
      <RecentEvents sensor={sensor} account={account} />
    </main>
  );
}
