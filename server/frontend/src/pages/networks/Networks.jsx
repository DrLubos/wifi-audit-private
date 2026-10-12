// Networks (/networks, boards/Networks.dc.html): rogue banner, networks and
// their security over time with the security changes, managed networks,
// clients heatmap, friendly and other networks. Filters sensor + range;
// page-local: view (heatmap) and page (other networks) in the URL, not
// carried by the navigation links.

import { Link, useSearchParams } from "react-router-dom";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useFilters } from "../../hooks/useFilters.js";
import { useNow } from "../../hooks/useNow.js";
import { rangeText } from "../overview/period.js";
import ClientsHeatmap from "./ClientsHeatmap.jsx";
import FriendlyTable from "./FriendlyTable.jsx";
import ManagedTable from "./ManagedTable.jsx";
import NetworkCharts from "./NetworkCharts.jsx";
import OtherTable from "./OtherTable.jsx";
import Rogue from "./Rogue.jsx";
import "./networks.css";

export default function Networks() {
  const { sensor, range } = useFilters();
  const account = useAccount();
  const now = useNow(30_000);
  const [params, setParams] = useSearchParams();
  const page = Math.max(1, Number.parseInt(params.get("page") ?? "1", 10) || 1);
  const view = params.get("view") === "week" ? "week" : "days";
  const setLocal = (k, v) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (v == null) next.delete(k); else next.set(k, String(v));
    return next;
  }, { replace: true });

  const [nk, nu] = ep.networks(sensor, range, page);
  const net = useQuery(nk, nu);
  const [tk, tu] = ep.networksTimeseries(sensor, range);
  const ts = useQuery(tk, tu);
  const d = net.data;
  const tz = account.tz;
  return (
    <main className="content v2">
      <div className="page-head">
        <h1>Networks</h1>
        <span className="muted">
          BSSIDs grouped by SSID · {d ? rangeText(range, d.period.from, now, tz) : "…"}. A <b>managed</b> network is
          yours: its security is checked against a policy and any unknown BSSID using its name raises an alert.
          A <b>friendly</b> network belongs to someone else but is known and legitimate: it is not checked, it is
          only kept out of the unknown list. Both are set in <Link to="/settings/networks">Settings</Link>.
        </span>
      </div>
      {d && <Rogue rogue={d.rogue} tz={tz} />}
      <NetworkCharts ts={ts} changes={d?.security_changes} tz={tz} now={now} />
      {net.error && <ErrorBox error={net.error} />}
      {!d && !net.error && <div className="card table-card"><Skeleton height={160} /></div>}
      {d && <ManagedTable managed={d.managed} now={now} tz={tz} />}
      <ClientsHeatmap sensor={sensor} range={range} view={view} setView={(v) => setLocal("view", v === "days" ? null : v)} />
      {d && <FriendlyTable friendly={d.friendly} />}
      {d && <OtherTable other={d.other} page={page} setPage={(p) => setLocal("page", p > 1 ? p : null)} />}
    </main>
  );
}
