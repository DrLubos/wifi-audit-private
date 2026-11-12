// The banner of discard mode (schema 20, docs/discard-mode.md) under the top
// bar of every page while a sensor's window is on: its uploads are accepted
// and not stored, the window is excluded from loss, outages and coverage.
// The text comes from status.js discardBanners(); "Manage" opens Settings >
// Sensors with that sensor selected (turn it off early there).

import { Link } from "react-router-dom";
import { useAccount } from "../hooks/useAccount.js";
import { useNow } from "../hooks/useNow.js";
import { discardBanners } from "./status.js";

export default function DiscardBanner() {
  const account = useAccount();
  const now = useNow(30_000);
  const banners = discardBanners(account.sensors, now, account.tz);
  if (!banners.length) return null;
  return (
    <div className="v2 discard-banners">
      {banners.map((b) => (
        <div key={b.sensor} className="discard-banner" role="status">
          <span className="stack">
            <span className="em">{b.head}</span>
            <span className="small">{b.text}</span>
          </span>
          <Link to={b.href} className="btn2">Manage</Link>
        </div>
      ))}
    </div>
  );
}
