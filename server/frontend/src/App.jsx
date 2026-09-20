import { Link, NavLink, Outlet } from "react-router-dom";
import HealthBadge from "./components/HealthBadge";

// Shell: header with navigation and the api/database indicator; pages render below.
export default function App() {
  return (
    <>
      <header className="topbar">
        <Link to="/" className="brand">wifi-audit</Link>
        <nav>
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/aps">Access points</NavLink>
        </nav>
        <span className="spacer" />
        <HealthBadge />
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
