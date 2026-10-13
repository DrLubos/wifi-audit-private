// Routes (PLAN.md 8.1). Every page is the new dashboard's; the first dashboard
// (src/legacy, /api/overview ...) was removed in phase 5c and its addresses
// (/detections, /legacy) redirect. The route handle names the global filters a
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
      { path: "aps", element: <ApList />, handle: { filters: PERIOD, title: "Access points" } },
      // /aps/<a Kismet device key> (the first dashboard's links) is redirected to the BSSID by ApDetail
      { path: "aps/:id", element: <ApDetail />, handle: { title: "Access point" } },
      // the first dashboard's pages are gone (phase 5c): their addresses lead to the new ones
      { path: "detections", element: <Navigate to="/events" replace /> },
      { path: "legacy", element: <Navigate to="/" replace /> },
      { path: "*", element: <NotFound />, handle: { title: "Not found" } },
    ],
  },
]);

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
