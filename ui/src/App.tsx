import { lazy, Suspense, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Identity } from "./api";
import { useAuth, useStatus, useTheme, useVersion, type ThemeChoice } from "./hooks";
import { LoginPage } from "./pages/Login";
import { OrganizationPage } from "./pages/Organization";
import { OverviewPage } from "./pages/Overview";
import { LivePage } from "./pages/Live";
import { TrafficPage } from "./pages/Traffic";
import { RequestsPage } from "./pages/Requests";
import { RequestPage } from "./pages/Request";
import { SessionsPage } from "./pages/Sessions";
import { SessionPage } from "./pages/Session";
import { AgentsPage } from "./pages/Agents";
import { ServerStatsPage } from "./pages/ServerStats";
import { SettingsPage } from "./pages/Settings";
import { TerminalsPage } from "./pages/Terminals";
import { PairPage } from "./pages/Pair";
import { MachinesPage } from "./pages/Machines";
import { ApproveDevicePage } from "./pages/ApproveDevice";
import { MachinePage } from "./pages/Machine";
import { DownloadsPage } from "./pages/Downloads";
import { DocsPage } from "./pages/Docs";
import { Icon } from "./components/Icon";
import { RestartButton } from "./components/Restart";
import { AttentionAlerts, waitingAgents } from "./components/Attention";
import { isGroup, PARTS, type NavLinkItem } from "./navigation";

// The terminal viewer brings xterm.js; load it when visited.
const TerminalPage = lazy(() => import("./pages/Terminal").then((module) => ({ default: module.TerminalPage })));

// Pages from before model servers lived on their machine's page: they go to the machines.
const MOVED = ["/models", "/hosts", "/server", "/providers", "/strata", "/docker", "/containers"];

export function App() {
  const auth = useAuth();
  if (!auth.data?.identity || auth.error) {
    if (window.location.pathname === "/get") return <PublicDownloads />;
    if (window.location.pathname === "/help" || window.location.pathname.startsWith("/help/")) return <PublicDocs />;
  }
  if (auth.isPending) return null;
  if (auth.error) return <main className="login"><section className="login-card panel"><h1>Sign-in unavailable</h1><p role="alert" className="bad">{auth.error.message}</p></section></main>;
  if (!auth.data?.identity) {
    return <LoginPage />;
  }
  return <Shell identity={auth.data?.identity ?? null} />;
}

function Shell({ identity }: { identity: Identity | null }) {
  const client = useQueryClient();
  const status = useStatus();
  const [theme, setTheme] = useTheme();
  const [menuOpen, setMenuOpen] = useState(false);
  const { pathname } = useLocation();
  // A narrow screen shows the rail as a menu; going somewhere closes it. Opening it adds a
  // history entry, so Back (Android's, or the browser's) closes the menu, not the page.
  useEffect(() => setMenuOpen(false), [pathname]);
  useEffect(() => {
    const popped = () => setMenuOpen(false);
    window.addEventListener("popstate", popped);
    return () => window.removeEventListener("popstate", popped);
  }, []);
  const openMenu = () => {
    window.history.pushState({ ...window.history.state, menu: true }, "");
    setMenuOpen(true);
  };
  const closeMenu = () => (window.history.state?.menu ? window.history.back() : setMenuOpen(false));
  const parts = PARTS;
  const inFlight = status.data?.current_requests.length ?? 0;
  // A sign-in that has lapsed gets a 401 here; stop asking until the page is reloaded.
  const terminals = useQuery({
    queryKey: ["terminals"],
    queryFn: api.terminals,
    retry: false,
    refetchInterval: (query) => (query.state.status === "error" ? false : 5000),
  });
  const machines = useQuery({
    queryKey: ["machines"],
    queryFn: api.machines,
    retry: false,
    refetchInterval: (query) => (query.state.status === "error" ? false : 15000),
  });
  const counts = {
    "/machines": machines.data?.machines.filter((machine) => machine.connected).length ?? 0,
    "/live": inFlight,
    "/terminals": terminals.data?.terminals.filter((item) => item.connected && !item.ended_at).length ?? 0,
  };
  // Running agents' count turns into an alert while some of them wait for their user.
  const waiting = waitingAgents(terminals.data?.terminals).length;
  const wordmark = (
    <NavLink to="/" className="wordmark" end>
      <svg viewBox="0 0 32 32" aria-hidden="true">
        <rect width="32" height="32" rx="9" />
        <path d="M8 11h6l4 10h6" />
      </svg>
      <span>AI Control Mesh</span>
    </NavLink>
  );
  return (
    <div className={`shell ${menuOpen ? "menu-open" : ""}`}>
      <header className="topbar">
        {wordmark}
        <span className={`connection ${status.isError ? "offline" : ""}`} title={status.data?.listen_address}>
          <span className="dot" aria-hidden="true" />
          {status.isError ? "Unreachable" : `${status.data?.active_sessions ?? 0} active`}
        </span>
        <button type="button" className="icon-button" aria-label="Menu" aria-expanded={menuOpen} onClick={menuOpen ? closeMenu : openMenu}>
          <Icon name={menuOpen ? "close" : "menu"} size={22} />
        </button>
      </header>
      <div className="scrim" aria-hidden="true" onClick={closeMenu} />
      <aside className="rail">
        {wordmark}
        <nav aria-label="Main">
          <div className="nav-group">
            <NavLink to="/" end>
              <Icon name="home" />
              <span className="nav-label">Overview</span>
            </NavLink>
          </div>
          {parts.map((part) => (
            <div key={part.id} className={`nav-group part-${part.id}`} role="group" aria-labelledby={`nav-${part.id}`}>
              <p className="nav-heading" id={`nav-${part.id}`} title={part.summary}>
                {part.label}
              </p>
              {part.items.map((item) =>
                isGroup(item) ? (
                  <div key={item.label} className="nav-sub" role="group" aria-label={item.label}>
                    <p className="nav-sub-heading" aria-hidden="true">
                      {item.icon && <Icon name={item.icon} />}
                      {item.label}
                    </p>
                    {item.items.map((link) => (
                      <RailLink key={link.to} item={link} counts={counts} />
                    ))}
                  </div>
                ) : (
                  <RailLink key={item.to} item={item} counts={counts} waiting={item.to === "/terminals" ? waiting : 0} />
                ),
              )}
              {part.id === "machines" && (
                <Link to="/machines#add" className="nav-add">
                  <Icon name="plus" />
                  <span className="nav-label">Add a machine</span>
                </Link>
              )}
            </div>
          ))}
        </nav>
        <div className="rail-foot">
          <Link to="/get" className="rail-download">
            <Icon name="download" size={16} />
            <span>Get the apps: agent, agent-machine, Android</span>
          </Link>
          <Link to="/help" className="rail-docs">
            <Icon name="book" size={16} />
            <span>Documentation</span>
          </Link>
          <p className={`connection ${status.isError ? "offline" : ""}`}>
            <span className="dot" aria-hidden="true" />
            <span>
              {status.isError
                ? "Proxy unreachable"
                : status.data
                  ? `${status.data.active_sessions} active ${status.data.active_sessions === 1 ? "session" : "sessions"}`
                  : "Connecting"}
              {status.data && <span className="mono subtle connection-address">{status.data.listen_address}</span>}
            </span>
          </p>
          <ServerVersion />
          {identity && (
            <div className="signed-in">
              <span className="avatar" aria-hidden="true">
                {identity.user.slice(0, 1).toUpperCase()}
              </span>
              <span className="signed-in-name">
                {identity.user}
                <small>{identity.role === "admin" ? "Admin" : "Member"}</small>
              </span>
              <button type="button" className="icon-button" title="Sign out" aria-label="Sign out" onClick={() => api.logout().then(() => client.invalidateQueries())}>
                <Icon name="logout" />
              </button>
            </div>
          )}
          <div className="theme-switch" role="radiogroup" aria-label="Theme">
            {(["system", "light", "dark"] as ThemeChoice[]).map((choice) => (
              <button
                key={choice}
                type="button"
                role="radio"
                aria-checked={theme === choice}
                title={`${choice[0].toUpperCase()}${choice.slice(1)} theme`}
                onClick={() => setTheme(choice)}
              >
                <Icon name={choice === "system" ? "monitor" : choice === "light" ? "sun" : "moon"} size={16} />
              </button>
            ))}
          </div>
        </div>
      </aside>
      <main className="content">
        {status.data?.restart_required && (
          <p className="banner warn row wrap">
            <span>The listen address was changed in Settings. Restart the proxy to use it.</span>
            <RestartButton status={status.data} label="Restart now…" />
          </p>
        )}
        <AttentionAlerts terminals={terminals.data?.terminals} />
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/traffic" element={<TrafficPage />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/requests/:sessionId/:requestId" element={<RequestPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:sessionId" element={<SessionPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/terminals" element={<TerminalsPage />} />
          <Route path="/machines" element={<MachinesPage />} />
          <Route path="/approve-device" element={<ApproveDevicePage />} />
          <Route path="/machines/:id" element={<MachinePage />} />
          <Route path="/machines/:id/:section" element={<MachinePage />} />
          <Route
            path="/terminals/:sessionId"
            element={
              <Suspense fallback={<p className="subtle">Loading the terminal…</p>}>
                <TerminalPage />
              </Suspense>
            }
          />
          <Route path="/pair" element={<PairPage />} />
          {MOVED.map((path) => (
            <Route key={path} path={path} element={<Navigate to="/machines" replace />} />
          ))}
          <Route path="/server-stats" element={<ServerStatsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/organization" element={<OrganizationPage />} />
          <Route path="/get" element={<DownloadsPage />} />
          {/* Not /docs: that's the server's OpenAPI pages. */}
          <Route path="/help" element={<DocsPage />} />
          <Route path="/help/:page" element={<DocsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

/** A rail link, with a count beside Live (requests streaming) and Running agents (connected terminals, or those waiting for you). */
/** The server's build, so you can tell which one you're looking at and whether machines match it. */
function ServerVersion() {
  const health = useVersion();
  if (!health.data?.version) return null;
  return (
    <p className="rail-version subtle" title={`Protocol ${health.data.protocol ?? "?"}`}>
      Version <span className="mono">{health.data.version}</span>
    </p>
  );
}

function RailLink({ item, counts, waiting = 0 }: { item: NavLinkItem; counts: Record<string, number>; waiting?: number }) {
  const count = counts[item.to] ?? 0;
  return (
    <NavLink to={item.to} end={item.end}>
      {item.icon && <Icon name={item.icon} />}
      <span className="nav-label">{item.label}</span>
      {waiting > 0 ? (
        <span className="nav-count attention-count" title={`${waiting} waiting for you`}>
          {waiting}
        </span>
      ) : (
        count > 0 && <span className="nav-count">{count}</span>
      )}
    </NavLink>
  );
}

/** The documentation before signing in, with the console's router for its links between pages. */
function PublicDocs() {
  return (
    <div className="public-page">
      <header className="public-head">
        <a href="/" className="wordmark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="9" />
            <path d="M8 11h6l4 10h6" />
          </svg>
          <span>AI Control Mesh</span>
        </a>
        <a className="button" href="/">
          Sign in
        </a>
      </header>
      <main className="content">
        <Routes>
          <Route path="/help" element={<DocsPage />} />
          <Route path="/help/:page" element={<DocsPage />} />
        </Routes>
      </main>
    </div>
  );
}

/** The Downloads page before signing in: the files are public, so a phone can get the app first. */
function PublicDownloads() {
  return (
    <div className="public-page">
      <header className="public-head">
        <a href="/" className="wordmark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="9" />
            <path d="M8 11h6l4 10h6" />
          </svg>
          <span>AI Control Mesh</span>
        </a>
        <a className="button" href="/">
          Sign in
        </a>
      </header>
      <main className="content">
        <DownloadsPage standalone />
      </main>
    </div>
  );
}
