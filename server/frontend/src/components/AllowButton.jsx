// "Add to allowed BSSIDs": the BSSID joins the allowed list of the managed
// network it broadcasts (POST /api/v2/settings/networks/{id}/allowed; the
// events are re-derived). The rule event and the Networks banner use it.

import { useState } from "react";
import { ep } from "../data/keys.js";
import { useQuery } from "../data/query.js";
import { useWrite } from "../data/write.js";

const LOCKED = "Unlock with the write token in Settings first";

export default function AllowButton({ bssid, ssid, className = "btn2" }) {
  const w = useWrite();
  const [k, u] = ep.settingsNetworks();
  const q = useQuery(k, u);
  const [state, setState] = useState({ busy: false, done: false, error: null });
  const net = q.data?.networks.find((n) => n.ssid === ssid && n.kind === "managed");
  const allowed = net?.allowed_bssids.includes(bssid);
  const why = !w.unlocked ? LOCKED : !net ? `${ssid} is not a managed network` : undefined;
  const go = async () => {
    setState({ busy: true, done: false, error: null });
    try {
      await w.send("POST", `/api/v2/settings/networks/${net.id}/allowed`, { bssid },
                   ["settings.networks", "networks", "events", "event|", "aps", "ap|", "overview.status"]);
      setState({ busy: false, done: true, error: null });
    } catch (e) {
      setState({ busy: false, done: false, error: e.message });
    }
  };
  if (allowed || state.done) return <span className="b ok">Allowed in {ssid}</span>;
  return (
    <>
      <button type="button" className={className} disabled={Boolean(why) || state.busy} title={why ?? `allow ${bssid} in ${ssid}`}
              onClick={go}>
        {state.busy ? "Adding…" : "Add to allowed BSSIDs"}
      </button>
      {state.error && <span className="bad-text small" role="alert">{state.error}</span>}
    </>
  );
}
