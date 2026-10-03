import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useStatus, useTheme, type ThemeChoice } from "./hooks";
import { OverviewPage } from "./pages/Overview";
import { RequestsPage } from "./pages/Requests";
import { SessionsPage } from "./pages/Sessions";
import { AgentsPage } from "./pages/Agents";
import { ModelsPage } from "./pages/Models";
import { SettingsPage } from "./pages/Settings";

const NAVIGATION = [
  { to: "/", label: "Overview", end: true },
  { to: "/requests", label: "Requests" },
  { to: "/sessions", label: "Sessions" },
  { to: "/agents", label: "Agents" },
  { to: "/models", label: "Models" },
  { to: "/settings", label: "Settings" },
];

export function App() {
  const status = useStatus();
  const [theme, setTheme] = useTheme();
  const inFlight = status.data?.current_requests.length ?? 0;
  return (
    <div className="shell">
      <aside className="rail">
        <div className="wordmark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" />
            <path d="M8 11h6l4 10h6" />
          </svg>
          <span>AI Proxy</span>
        </div>
        <nav aria-label="Main">
          {NAVIGATION.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.label}
              {item.to === "/requests" && inFlight > 0 && <span className="nav-count">{inFlight}</span>}
            </NavLink>
          ))}
        </nav>
        <div className="rail-foot">
          <p className={`connection ${status.isError ? "offline" : ""}`}>
            <span className="dot" aria-hidden="true" />
            {status.isError
              ? "Proxy unreachable"
              : status.data
                ? `${status.data.active_sessions} active ${status.data.active_sessions === 1 ? "session" : "sessions"}`
                : "Connecting"}
          </p>
          {status.data && <p className="mono subtle">{status.data.listen_address}</p>}
          <label className="theme-select">
            Theme
            <select value={theme} onChange={(event) => setTheme(event.target.value as ThemeChoice)}>
              <option value="system">System</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </select>
          </label>
        </div>
      </aside>
      <main className="content">
        {status.data?.restart_required && (
          <p className="banner warn">
            The listen address was changed in Settings. Restart the proxy to use it.
          </p>
        )}
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
