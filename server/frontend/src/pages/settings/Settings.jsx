// Settings (/settings/:tab, boards/Settings.dc.html, SettingsSensors.dc.html):
// one route per tab - Networks, Classification, Detectors, Sensors (Users
// later) - with the write-token box on every tab. Reading is public; saving
// needs the token (data/write.js), each save is one audited transaction.

import { Link, Navigate, useParams } from "react-router-dom";
import WriteTokenBox from "../../components/WriteTokenBox.jsx";
import ClassificationTab from "./ClassificationTab.jsx";
import DetectorsTab from "./DetectorsTab.jsx";
import NetworksTab from "./NetworksTab.jsx";
import SensorsTab from "./SensorsTab.jsx";
import { TABS } from "./settings.js";
import "./settings.css";

const PAGES = { networks: NetworksTab, classification: ClassificationTab, detectors: DetectorsTab, sensors: SensorsTab };

export default function Settings() {
  const { tab } = useParams();
  const Page = PAGES[tab];
  if (!Page) return <Navigate to="/settings/networks" replace />;
  return (
    <main className="content v2 settings" style={{ gap: 20 }}>
      <h1>Settings</h1>
      <nav aria-label="Settings sections" className="set-tabs">
        {TABS.map(([k, label]) => (
          <Link key={k} to={`/settings/${k}`} aria-current={k === tab ? "page" : undefined}>{label}</Link>
        ))}
        <span className="set-tab-later" aria-disabled="true">Users (later)</span>
      </nav>
      <WriteTokenBox />
      <Page />
    </main>
  );
}
