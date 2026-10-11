// Routes (PLAN.md 8.1). Dashboard v2 replaces the previous pages one by one:
// until a page is rebuilt, its link shows the previous page inside the new
// shell (Access points, AP detail, Detections) or a note (Networks, Spectrum,
// Sensors, Settings); the previous Overview stays at /legacy. The route
// handle names the global filters a page uses.

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/charts.css";
import Shell from "./app/Shell.jsx";
import Pending from "./app/Pending.jsx";
import NotFound from "./app/NotFound.jsx";
import Legacy from "./legacy/Legacy.jsx";
import OldOverview from "./legacy/pages/Overview.jsx";
import Inventory from "./legacy/pages/Inventory.jsx";
import ApDetail from "./legacy/pages/ApDetail.jsx";
import Detections from "./legacy/pages/Detections.jsx";

const DETECTIONS = { to: "/detections", label: "Detections" };

const router = createBrowserRouter([
  {
    element: <Shell />,
    children: [
      { index: true, element: <Legacy><OldOverview /></Legacy>, handle: { title: "Overview" } },
      { path: "events", element: <Pending title="Events" phase="phase 4e" legacy={DETECTIONS} />,
        handle: { title: "Events" } },
      { path: "networks", element: <Pending title="Networks" phase="phase 4b" />, handle: { title: "Networks" } },
      { path: "spectrum", element: <Pending title="Spectrum" phase="phase 4c" />, handle: { title: "Spectrum" } },
      { path: "sensors", element: <Pending title="Sensors" phase="phase 4f" />, handle: { title: "Sensors" } },
      { path: "settings/*", element: <Pending title="Settings" phase="phase 5" />, handle: { title: "Settings" } },
      // the previous dashboard, until phase 4d (access points) and 4e (events)
      { path: "aps", element: <Legacy><Inventory /></Legacy>, handle: { title: "Access points" } },
      { path: "aps/:deviceKey", element: <Legacy><ApDetail /></Legacy>, handle: { title: "Access point" } },
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
