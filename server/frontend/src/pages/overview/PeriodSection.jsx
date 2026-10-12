// "Selected period" (sensor + range, Main board): the three tiles with
// sparklines, access points over time (fixed / mobile, event dots, hatched
// gaps), the security of the APs seen. One request (/api/v2/overview/period).

import { Link } from "react-router-dom";
import { xLabels } from "../../charts/geometry.js";
import SegmentBar from "../../charts/SegmentBar.jsx";
import Sparkline from "../../charts/Sparkline.jsx";
import StackedColumns from "../../charts/StackedColumns.jsx";
import { LEVELS } from "../../app/status.js";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { fmtInt } from "../../format.js";
import { chartSub, collecting, columnNotes, labelsOf, majorEvents, rangeText, tileTexts, warmupNote } from "./period.js";

const UNIT_ARIA = { "1h": "hour", "6h": "6-hour window", "1d": "day" };

function Tile({ label, link, to, value, unit, sub, spark }) {
  return (
    <div className="card tile">
      <div className="tile-head">
        <span className="muted small">{label}</span>
        <Link to={to}>{link}</Link>
      </div>
      <div className="tile-body">
        <span className="tile-value">{value} {unit && <span className="tile-unit">{unit}</span>}</span>
        {spark}
      </div>
      <span className="muted small">{sub}</span>
    </div>
  );
}

function Tiles({ d, labels, on, tz }) {
  const t = d.tiles;
  const text = tileTexts(d, tz);
  const unit = UNIT_ARIA[d.period.bucket];
  const loadGap = (i) => (d.buckets[i].load_missing ? "client counts were not recorded" : "no sensor collecting");
  return (
    <section aria-label="Period indicators" className="tiles">
      <Tile label="New APs" link="Access points" to="/aps" value={fmtInt(t.new_aps.total)} sub={text.newSub}
            spark={<Sparkline values={t.new_aps.series} on={on} labels={labels} unit="new APs"
                              notes={[warmupNote(d)]} ariaLabel={`New APs per ${unit}`} />} />
      <Tile label="Open networks nearby" link="Networks" to="/networks" value={fmtInt(t.open_nearby.total)}
            sub={text.openSub}
            spark={<Sparkline values={t.open_nearby.series} on={on} labels={labels} unit="open APs seen"
                              ariaLabel={`Open APs seen per ${unit}`} />} />
      <Tile label="Clients on managed APs" link="Weekly pattern" to="/networks"
            value={t.clients_managed.peak == null ? "–" : fmtInt(t.clients_managed.peak)} unit="peak"
            sub={text.clientsSub}
            spark={<Sparkline values={t.clients_managed.series} on={on} labels={labels} unit="clients (hourly peak)"
                              gapText={loadGap} ariaLabel={`Peak clients per ${unit}`} />} />
    </section>
  );
}

function ApsOverTime({ d, labels, on, sensor, sensors, tz }) {
  const unit = UNIT_ARIA[d.period.bucket];
  const series = [
    { key: "fixed", label: "fixed APs", color: "var(--c1)", values: d.aps.fixed },
    { key: "mobile", label: "mobile APs", color: "var(--c2)", values: d.aps.mobile },
  ];
  const notes = labels.map((_, i) => columnNotes(d, i, sensor, sensors));
  const aria = (i) => (on[i]
    ? `${labels[i]}: ${fmtInt(d.aps.fixed[i] ?? 0)} fixed, ${fmtInt(d.aps.mobile[i] ?? 0)} mobile APs`
    : `${labels[i]}: no sensor collecting`);
  return (
    <section className="card panel" aria-labelledby="act-h">
      <div className="card-head">
        <div className="card-title">
          <h3 id="act-h">Access points over time</h3>
          <span className="muted small">{chartSub(d, labels, on)}</span>
        </div>
        <div className="lg">
          <span><span className="sw" style={{ background: "var(--c1)" }} />fixed APs</span>
          <span><span className="sw" style={{ background: "var(--c2)" }} />mobile APs (hotspots)</span>
          <span><span className="evdot" />event (medium or higher)</span>
          <span><span className="sw hatch" />sensor not collecting</span>
        </div>
      </div>
      <StackedColumns series={series} on={on} labels={labels} xlabels={xLabels(d.buckets, d.period.bucket, tz)}
                      notes={notes} events={majorEvents(d)} columnAria={aria}
                      ariaLabel={`Stacked columns of fixed and mobile APs per ${unit}; hover or focus a column for exact values`} />
      <span className="muted chart-note">
        <b style={{ color: "var(--text)", fontWeight: 500 }}>Mobile AP</b> = randomized (locally administered)
        BSSID, phone vendor, or present for less than 2 h.
      </span>
    </section>
  );
}

function Security({ d }) {
  const total = d.security.reduce((a, r) => a + r.aps, 0);
  const rows = d.security.map((r) => ({
    key: r.level, label: LEVELS[r.level]?.label ?? r.label, n: r.aps, share: r.share,
    color: LEVELS[r.level]?.color ?? "var(--lvl-unknown)",
  }));
  const aria = `Security of ${fmtInt(total)} APs: ${rows.map((r) => `${r.label} ${Math.round(r.share * 100)} %`).join(", ")}`;
  return (
    <section className="card panel" aria-labelledby="sec-h">
      <div className="card-head">
        <h3 id="sec-h">Security of nearby APs</h3>
        <span className="muted small">{fmtInt(total)} APs seen in the period · hover a segment to highlight it</span>
      </div>
      <SegmentBar rows={rows} ariaLabel={aria} />
    </section>
  );
}

export default function PeriodSection({ sensor, range, account, now }) {
  const [key, url] = ep.overviewPeriod(sensor, range);
  const q = useQuery(key, url);
  const d = q.data;
  const tz = account.tz;
  const sensorLabel = sensor === "all" ? "all sensors" : `sensor ${sensor}`;
  const labels = d ? labelsOf(d, now, tz) : null;
  const on = d ? collecting(d.buckets) : null;
  return (
    <>
      <div className="sec" style={{ marginTop: 6 }}>
        <h2>Selected period</h2>
        <span className="muted small">
          {d ? rangeText(d.filters.range, d.period.from, now, tz) : "…"} · {sensorLabel}
        </span>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      {!d && !q.error && (
        <>
          <div className="tiles">
            {[0, 1, 2].map((i) => <div key={i} className="card tile"><Skeleton height={96} /></div>)}
          </div>
          <div className="card panel"><Skeleton height={240} /></div>
        </>
      )}
      {d && (
        <div className={q.isPrevious ? "stale" : undefined} style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <Tiles d={d} labels={labels} on={on} tz={tz} />
          <ApsOverTime d={d} labels={labels} on={on} sensor={sensor} sensors={account.sensors} tz={tz} />
          <Security d={d} />
        </div>
      )}
    </>
  );
}
