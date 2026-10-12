// Spectrum and channels (/spectrum, boards/Spectrum.dc.html): channel
// occupancy of the band (fixed APs, each on its primary channel), the
// channel plan issues and the utilization heatmap. Filters sensor + range;
// page-local: band (2.4 / 5) and view (average day / timeline) in the URL,
// not carried by the navigation links.

import { useSearchParams } from "react-router-dom";
import ErrorBox from "../../components/ErrorBox.jsx";
import Skeleton from "../../components/Skeleton.jsx";
import { ep } from "../../data/keys.js";
import { useQuery } from "../../data/query.js";
import { useAccount } from "../../hooks/useAccount.js";
import { useFilters } from "../../hooks/useFilters.js";
import { useNow } from "../../hooks/useNow.js";
import { fmtInt } from "../../format.js";
import { rangeText } from "../overview/period.js";
import ChannelColumns from "./ChannelColumns.jsx";
import { bandName, issueItems, LEGEND, occNote, occupancy } from "./spectrum.js";
import UtilizationHeatmap from "./UtilizationHeatmap.jsx";
import "./spectrum.css";

export default function Spectrum() {
  const { sensor, range } = useFilters();
  const account = useAccount();
  const now = useNow(30_000);
  const [params, setParams] = useSearchParams();
  const band = params.get("band") === "5" ? "5" : "2.4";
  const view = params.get("view") === "timeline" ? "timeline" : "day";
  const setLocal = (k, v) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (v == null) next.delete(k); else next.set(k, String(v));
    return next;
  }, { replace: true });

  const [key, url] = ep.spectrum(sensor, range, band);
  const q = useQuery(key, url);
  const d = q.data;
  const tz = account.tz;
  const items = d ? occupancy(d.channels, band) : [];
  return (
    <main className="content v2">
      <div className="spec-head">
        <div className="page-head">
          <h1>Spectrum and channels</h1>
          <span className="muted">
            Channel occupancy, overlap and utilization as reported by the APs themselves
            {d ? ` · ${rangeText(range, d.period.from, now, tz)}` : ""}
          </span>
        </div>
        <div role="group" aria-label="Band" className="seg seg-lg">
          <button type="button" aria-pressed={band === "2.4"} onClick={() => setLocal("band", null)}>2.4 GHz</button>
          <button type="button" aria-pressed={band === "5"} onClick={() => setLocal("band", "5")}>5 GHz</button>
        </div>
      </div>
      {q.error && <ErrorBox error={q.error} />}
      <section className="card panel" aria-labelledby="occ-h" style={{ gap: 12 }}>
        <div className="card-head">
          <h2 id="occ-h">
            Channel occupancy – {bandName(band)}{d ? ` (${fmtInt(d.aps)} APs, hotspots excluded)` : ""}
          </h2>
          <div className="lg">
            <span><span className="sw" style={{ background: "var(--c1)" }} />{LEGEND[band][0]}</span>
            <span><span className="sw" style={{ background: "var(--c2)" }} />{LEGEND[band][1]}</span>
          </div>
        </div>
        {!d && !q.error && <Skeleton height={250} />}
        {d && items.length === 0 && <p className="muted small" style={{ margin: 0 }}>No fixed AP on {bandName(band)} in this period.</p>}
        {d && items.length > 0 && (
          <div className={q.isPrevious ? "stale" : undefined}>
            <ChannelColumns items={items}
                            ariaLabel="Columns of APs per channel; hover or focus a column for details" />
          </div>
        )}
        {d && <span className="muted tiny">{occNote(band, d.current_channel_aps)}</span>}
      </section>
      <section className="card panel" aria-labelledby="iss-h" style={{ gap: 12 }}>
        <h2 id="iss-h">Channel plan issues</h2>
        {!d && !q.error && <Skeleton height={90} />}
        {d && (
          <div className="issues">
            {issueItems(d.issues, band).map((it) => (
              <div className="issue" key={it.badge + it.text}>
                <span className={`b ${it.cls}`}>{it.badge}</span><span>{it.text}</span>
              </div>
            ))}
          </div>
        )}
      </section>
      <UtilizationHeatmap sensor={sensor} range={range} band={band} view={view} tz={tz} now={now}
                          setView={(v) => setLocal("view", v === "day" ? null : v)} />
    </main>
  );
}
