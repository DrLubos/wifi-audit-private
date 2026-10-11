// "Current status" (now, sensor filter): sensor cards, open events by
// severity, managed networks compliant, needs attention (Main board).

import { Link } from "react-router-dom";
import { ATTENTION_BG, sensorState, severitySummary } from "../../app/status.js";
import { SeverityBadge } from "../../components/Badge.jsx";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { fmtAgo, fmtDateTime, fmtInt, toDate } from "../../format.js";

const SHOWN = 6;

function lastPoll(s, now, tz) {
  const t = toDate(s.last_poll);
  if (!t) return "no poll yet";
  return now - t.getTime() < 3600e3 ? `last poll ${fmtAgo(t, now)}` : `last poll ${fmtDateTime(t, tz)}`;
}

function SensorCard({ s, account, now }) {
  const st = sensorState(s, account.offlineMin, now, account.tz);
  return (
    <div className="card sensor-card">
      <div className="stack">
        <span style={{ fontWeight: 600 }}>{s.name}</span>
        <span className="muted small">{[s.location, lastPoll(s, now, account.tz)].filter(Boolean).join(" · ")}</span>
      </div>
      <div className="badges">
        <span className={`b ${st.tone}`}>{st.badge}</span>
        {st.offline
          ? <Link to="/sensors" className="small">Sensor details</Link>
          : s.hop && <span className={`b ${s.hop.ok ? "ok" : "warn"}`}>Channels {s.hop.visited}/{s.hop.n}</span>}
      </div>
    </div>
  );
}

function OpenEvents({ st }) {
  return (
    <Link to="/events" className="card stat-card">
      <span className="muted small">Open events</span>
      <span className="big">{st.data ? fmtInt(st.data.open_events.total) : "–"}</span>
      <span className="muted tiny">{st.data ? severitySummary(st.data.open_events.by_severity) || "none" : "…"}</span>
    </Link>
  );
}

function Compliance({ st }) {
  const c = st.data?.compliance;
  return (
    <Link to="/networks" className="card stat-card">
      <span className="muted small">Managed networks compliant</span>
      <span className="big">{c ? `${c.met} / ${c.total}` : "–"}</span>
      {c && c.first_problem && <span className="problem">{c.first_problem.network}: {c.first_problem.reason}</span>}
      {c && !c.first_problem && (
        <span className="muted tiny">{c.total ? "every BSSID meets its policy" : "no managed network configured"}</span>
      )}
    </Link>
  );
}

function linkText(item) {
  if (item.kind === "sensor") return "Sensors";
  if (item.kind === "compliance") return "Networks";
  return "Investigate";
}

function Attention({ st, now }) {
  if (st.error && !st.data) return null;
  const items = st.data?.attention ?? [];
  const more = (st.data?.attention_total ?? 0) - Math.min(items.length, SHOWN);
  return (
    <section className="card attn" aria-labelledby="att-h">
      <h3 id="att-h">Needs attention</h3>
      {!st.data && <Skeleton height={64} />}
      {st.data && items.length === 0 && <span className="muted small">Nothing needs attention.</span>}
      <div className="attn-list">
        {items.slice(0, SHOWN).map((a, i) => (
          <div className="attn-item" key={a.event_id ?? `c${i}`} style={{ background: ATTENTION_BG[a.severity] }}>
            <SeverityBadge severity={a.severity} />
            <div className="attn-text">
              <span className="title">{a.title}</span>
              <span className="muted small">
                {a.bssid && <><span className="mono">{a.bssid}</span> · </>}
                {[a.kind !== "compliance" && a.network, a.detail, a.sensors?.join(", "), a.ts && fmtAgo(a.ts, now)]
                  .filter(Boolean).map((t) => `${t} · `).join("")}
                <Link to={a.link}>{linkText(a)}</Link>
              </span>
            </div>
          </div>
        ))}
      </div>
      {more > 0 && <Link to="/events" className="small">{fmtInt(more)} more open events</Link>}
    </section>
  );
}

export default function CurrentStatus({ sensor, account, now }) {
  const [key, url] = ep.overviewStatus(sensor);
  const st = useQuery(key, url);
  const sensors = account.sensors?.filter((s) => sensor === "all" || s.name === sensor);
  return (
    <>
      <section aria-label="Current status" className="status-row">
        {sensors
          ? sensors.map((s) => <SensorCard key={s.name} s={s} account={account} now={now} />)
          : <div className="card sensor-card"><Skeleton height={40} /></div>}
        <OpenEvents st={st} />
        <Compliance st={st} />
      </section>
      {st.error && <ErrorBox error={st.error} />}
      <Attention st={st} now={now} />
    </>
  );
}
