import { lazy, Suspense, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Identity } from "./api";
import { AccountMenu, useAccountChanges } from "./components/AccountMenu";
import { pollEvery, useAuth, useStatus, useTheme, useVersion, type ThemeChoice } from "./hooks";
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
import { AskPage } from "./pages/Ask";
import { SubscriptionsPage } from "./pages/Subscriptions";
import { ProvidersPage } from "./pages/Providers";
import { ServerStatsPage } from "./pages/ServerStats";
import { AccessLogPage } from "./pages/AccessLog";
import { ApiLogPage } from "./pages/ApiLog";
import { AuditLogPage } from "./pages/AuditLog";
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
import { isGroup, visibleParts, type NavLinkItem } from "./navigation";

// The terminal viewer brings xterm.js; load it when visited.
const TerminalPage = lazy(() => import("./pages/Terminal").then((module) => ({ default: module.TerminalPage })));
const TerminalGridPage = lazy(() => import("./pages/TerminalGrid").then((module) => ({ default: module.TerminalGridPage })));

// Pages from before model servers lived on their machine's page: they go to the machines.
// (/providers is a live page again and must not be here.)
const MOVED = ["/models", "/hosts", "/server", "/strata", "/docker", "/containers"];

export function App() {
  const auth = useAuth();
  useAccountChanges();
  // The last identity the server confirmed. A later check that fails (a phone's network blip, the
  // sign-in service slow while the token is refreshed) keeps it: tearing the console down would cut
  // off what's open, such as a model's streamed reply or a terminal. A check that answers signed out
  // still shows sign-in, and the next check (every 30 s) recovers from a failed one.
  const identity = auth.data?.identity ?? null;
  if (!identity) {
    if (window.location.pathname === "/get") return <PublicDownloads />;
    if (window.location.pathname === "/help" || window.location.pathname.startsWith("/help/")) return <PublicDocs />;
  }
  if (auth.isPending) return null;
  if (auth.error && !identity) return <main className="login"><section className="login-card panel"><h1>Sign-in unavailable</h1><p role="alert" className="bad">{auth.error.message}</p></section></main>;
  if (!identity) {
    return <LoginPage />;
  }
  return <Shell identity={identity} />;
}

function Shell({ identity }: { identity: Identity | null }) {
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
  const parts = visibleParts(Boolean(identity?.site_admin), identity?.role === "admin");
  const inFlight = status.data?.current_requests.length ?? 0;
  // After an error (a lapsed sign-in, a dropped network) these keep asking, more slowly, so the
  // counts come back by themselves; only a 403 stops them.
  const terminals = useQuery({
    queryKey: ["terminals", "open"],
    queryFn: () => api.terminals(true),
    retry: false,
    refetchInterval: pollEvery(5000),
  });
  const machines = useQuery({
    queryKey: ["machines"],
    queryFn: api.machines,
    retry: false,
    refetchInterval: pollEvery(15000, 30000),
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
            <NavLink to="/overview">
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
          {identity && <AccountMenu identity={identity} />}
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
        {status.data?.restart_required && identity?.site_admin && (
          <p className="banner warn row wrap">
            <span>The listen address was changed in Settings. Restart the proxy to use it.</span>
            <RestartButton status={status.data} label="Restart now…" />
          </p>
        )}
        <AttentionAlerts terminals={terminals.data?.terminals} />
        <Routes>
          {/* The console opens on the Live agent view: the agents running now. */}
          <Route path="/" element={<Navigate to="/live-agents" replace />} />
          <Route path="/overview" element={<OverviewPage />} />
          <Route path="/live" element={<LivePage />} />
          <Route path="/traffic" element={<TrafficPage />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/requests/:sessionId/:requestId" element={<RequestPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/sessions/:sessionId" element={<SessionPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/terminals" element={<TerminalsPage />} />
          <Route path="/live-agents" element={<Suspense fallback={<p>Loading terminals…</p>}><TerminalGridPage /></Suspense>} />
          <Route path="/terminals-beta" element={<Navigate to="/live-agents" replace />} />
          <Route path="/ask" element={<AskPage />} />
          <Route path="/subscriptions" element={<SubscriptionsPage />} />
          <Route path="/providers" element={<ProvidersPage />} />
          <Route path="/machines" element={<MachinesPage />} />
          <Route path="/approve-device" element={<ApproveDevicePage />} />
          <Route path="/machines/:id" element={<MachinePage />} />
          <Route path="/machines/:id/:section" element={<MachinePage />} />
          <Route path="/machines/:id/:section/:kind" element={<MachinePage />} />
          <Route path="/machines/:id/:section/:kind/:sub" element={<MachinePage />} />
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
          <Route path="/access-log" element={<AccessLogPage />} />
          <Route path="/api-log" element={<ApiLogPage />} />
          <Route path="/audit" element={<AuditLogPage />} />
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
