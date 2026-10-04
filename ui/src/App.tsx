import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useStatus, useTheme, type ThemeChoice } from "./hooks";
import { OverviewPage } from "./pages/Overview";
import { LivePage } from "./pages/Live";
import { TrafficPage } from "./pages/Traffic";
import { RequestsPage } from "./pages/Requests";
import { RequestPage } from "./pages/Request";
import { SessionsPage } from "./pages/Sessions";
import { SessionPage } from "./pages/Session";
import { AgentsPage } from "./pages/Agents";
import { ModelsPage } from "./pages/Models";
import { HostsPage } from "./pages/Hosts";
import { SettingsPage } from "./pages/Settings";

// Activity pages cover every route; the Ollama group only applies to local models.
const NAVIGATION: { heading?: string; hint?: string; items: { to: string; label: string; end?: boolean }[] }[] = [
  {
    items: [
      { to: "/", label: "Overview", end: true },
      { to: "/agents", label: "Start an agent" },
    ],
  },
  {
    heading: "Activity",
    hint: "All routes",
    items: [
      { to: "/live", label: "Live" },
      { to: "/traffic", label: "Traffic" },
      { to: "/requests", label: "Requests" },
      { to: "/sessions", label: "Sessions" },
    ],
  },
  {
    heading: "Ollama",
    hint: "Local models only",
    items: [
      { to: "/models", label: "Models" },
      { to: "/hosts", label: "Machines" },
    ],
  },
  { heading: "Setup", items: [{ to: "/settings", label: "Settings" }] },
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
          {NAVIGATION.map((group, index) => (
            <div key={group.heading ?? index} className="nav-group">
              {group.heading && (
                <p className="nav-heading" title={group.hint}>
                  {group.heading}
                  {group.hint && <span>{group.hint}</span>}
                </p>
              )}
              {group.items.map((item) => (
                <NavLink key={item.to} to={item.to} end={item.end}>
                  {item.label}
                  {item.to === "/live" && inFlight > 0 && <span className="nav-count">{inFlight}</span>}
                </NavLink>
              ))}
            </div>
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
          <Route path="/live" element={<LivePage />} />
          <Route path="/traffic" element={<TrafficPage />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/requests/:sessionId/:requestId" element={<RequestPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:sessionId" element={<SessionPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/hosts" element={<HostsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
