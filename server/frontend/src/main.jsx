// Routes (PLAN.md 8.1). Dashboard v2 replaces the previous pages one by one:
// every page is rebuilt (phase 5: Settings); the previous
// Overview (/legacy) and Detections (/detections) stay reachable under
// "Previous dashboard" until phase 5 (their AP links, /aps/<device key>,
// redirect to the new AP page). The route handle names the global filters a
// page uses.

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/charts.css";
import Shell from "./app/Shell.jsx";
import NotFound from "./app/NotFound.jsx";
import Overview from "./pages/overview/Overview.jsx";
import Networks from "./pages/networks/Networks.jsx";
import Spectrum from "./pages/spectrum/Spectrum.jsx";
import ApList from "./pages/aps/ApList.jsx";
import ApDetail from "./pages/aps/ApDetail.jsx";
import Events from "./pages/events/Events.jsx";
import Sensors from "./pages/sensors/Sensors.jsx";
import Settings from "./pages/settings/Settings.jsx";
import Legacy from "./legacy/Legacy.jsx";
import OldOverview from "./legacy/pages/Overview.jsx";
import Detections from "./legacy/pages/Detections.jsx";

const PERIOD = ["sensor", "range"];

const router = createBrowserRouter([
  {
    element: <Shell />,
    children: [
      { index: true, element: <Overview />, handle: { filters: PERIOD, title: "Overview" } },
      { path: "events", element: <Events />, handle: { filters: ["sensor"], title: "Events" } },
      { path: "networks", element: <Networks />, handle: { filters: PERIOD, title: "Networks" } },
      { path: "spectrum", element: <Spectrum />, handle: { filters: PERIOD, title: "Spectrum" } },
      { path: "sensors", element: <Sensors />, handle: { title: "Sensors" } },
      { path: "settings", element: <Navigate to="/settings/networks" replace />, handle: { title: "Settings" } },
      { path: "settings/:tab", element: <Settings />, handle: { title: "Settings" } },
      // the previous dashboard, until phase 4d (access points) and 4e (events)
      { path: "aps", element: <ApList />, handle: { filters: PERIOD, title: "Access points" } },
      { path: "aps/:id", element: <ApDetail />, handle: { title: "Access point" } },
      { path: "detections", element: <Legacy><Detections /></Legacy>, handle: { title: "Detections" } },
      { path: "legacy", element: <Legacy><OldOverview /></Legacy>, handle: { title: "Previous overview" } },
      { path: "*", element: <NotFound />, handle: { title: "Not found" } },
    ],
  },
]);

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
