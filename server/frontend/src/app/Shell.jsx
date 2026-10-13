// The layout of every page: sidebar, top bar, the page (<main className="content v2">).
// The tab title comes from the route handle.

import { useEffect } from "react";
import { Outlet, useMatches } from "react-router-dom";
import Sidebar from "./Sidebar.jsx";
import TopBar from "./TopBar.jsx";

export default function Shell() {
  const matches = useMatches();
  const title = [...matches].reverse().find((m) => m.handle?.title)?.handle.title;
  useEffect(() => {
    document.title = title ? `${title} · Wi-Fi Sensor` : "Wi-Fi Sensor";
  }, [title]);
  return (
    <div className="app">
      <Sidebar />
      <div className="main">
        <TopBar />
        <Outlet />
      </div>
    </div>
  );
}
