import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import App from "./App.jsx";
import Overview from "./pages/Overview.jsx";
import Inventory from "./pages/Inventory.jsx";
import ApDetail from "./pages/ApDetail.jsx";
import Detections from "./pages/Detections.jsx";
import "./index.css";

function NotFound() {
  return <p className="muted">Nothing here. <Link to="/">Overview</Link></p>;
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<App />}>
          <Route index element={<Overview />} />
          <Route path="aps" element={<Inventory />} />
          <Route path="aps/:deviceKey" element={<ApDetail />} />
          <Route path="detections" element={<Detections />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
