import { Link } from "react-router-dom";
import { isModelCall, requestKey, type RequestRecord, type Status } from "../api";
import { RouteBadge } from "../components/RouteBadge";
import { RoutingMap } from "../components/RoutingMap";
import { ThroughputChart } from "../components/ThroughputChart";
import { formatBytes, formatCompact, formatDuration, formatNumber, formatRelative } from "../format";
import { useRequests, useSessions, useStatus } from "../hooks";
import { ROUTE_ORDER, ROUTES, routeOf, type RouteKind } from "../routes";

export function OverviewPage() {
  const status = useStatus();
  const sessions = useSessions();
  const requests = useRequests();
  if (status.isError) return <Offline message={status.error.message} />;
  if (!status.data) return <p className="empty">Connecting to the proxy…</p>;
  const data = status.data;
  const live = data.current_requests;
  // Account agents also send sign-in, telemetry and MCP calls; those belong on the Traffic page.
  const modelCalls = data.recent_requests.filter(isModelCall);
  return (
    <div className="page">
      <header className="page-head">
        <h1>{live.length ? `${live.length} ${live.length === 1 ? "request" : "requests"} streaming` : "All quiet"}</h1>
        <p className="lede">
          Every agent request passes through this proxy, whether it goes to your Ollama machines, a hosted API, or the
          agent's own account.
        </p>
      </header>

      <RoutingMap status={data} sessions={sessions.data ?? []} />

      <RouteSummary status={data} records={requests.data ?? data.recent_requests} />

      <dl className="totals">
        <div>
          <dt>Requests</dt>
          <dd>{formatNumber(data.total_requests)}</dd>
        </div>
        <div>
          <dt>Tokens read</dt>
          <dd>{formatCompact(data.total_input_tokens)}</dd>
        </div>
        <div>
          <dt>Tokens written</dt>
          <dd>{formatCompact(data.total_output_tokens)}</dd>
        </div>
        <div>
          <dt>Sessions</dt>
          <dd>
            {formatNumber(data.active_sessions)}
            <small> open of {formatNumber(data.total_sessions)}</small>
          </dd>
        </div>
      </dl>

      {live.length > 0 && (
        <section className="section">
          <div className="section-head">
            <h2>Streaming now</h2>
            <Link to="/live">Watch the tokens live</Link>
          </div>
          <ul className="live-list">
            {live.map((item) => (
              <li key={requestKey(item)}>
                <span className="live-mark" aria-hidden="true" />
                <strong>{item.client}</strong>
                <span>{item.model}</span>
                <RouteBadge item={item} />
                <span className="subtle">
                  {item.ttft_ms == null ? "waiting for first token" : `first token ${formatDuration(item.ttft_ms)}`}
                </span>
                <span className="subtle">{formatBytes(item.response_bytes)} received</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="section">
        <div className="section-head">
          <h2>Generation speed</h2>
          <p className="subtle">Tokens per second for each recent request</p>
        </div>
        <ThroughputChart records={modelCalls} />
      </section>

      <section className="section">
        <div className="section-head">
          <h2>Latest model calls</h2>
          <Link to="/requests">Open request history</Link>
        </div>
        {modelCalls.length ? (
          <table className="table compact">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Agent</th>
                <th scope="col">Route</th>
                <th scope="col" className="num">First token</th>
                <th scope="col" className="num">Tokens/s</th>
                <th scope="col" className="num">When</th>
              </tr>
            </thead>
            <tbody>
              {modelCalls.slice(0, 8).map((record) => (
                <tr key={requestKey(record)}>
                  <td>
                    <Link to={`/requests?request=${encodeURIComponent(requestKey(record))}`}>{record.model || "Unknown model"}</Link>
                  </td>
                  <td>{record.client}</td>
                  <td>
                    <RouteBadge item={record} />
                  </td>
                  <td className="num">{formatDuration(record.ttft_ms)}</td>
                  <td className="num">{formatNumber(record.generation_tps)}</td>
                  <td className="num subtle">{formatRelative(record.timestamp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">
            <p>No requests yet. Start an agent from the Agents page to see traffic here.</p>
            <Link className="button primary" to="/agents">
              Choose an agent
            </Link>
          </div>
        )}
      </section>
    </div>
  );
}

const ROUTE_LINKS: Record<RouteKind, { to: string; label: string }[]> = {
  account: [{ to: "/traffic", label: "All traffic" }],
  ollama: [
    { to: "/models", label: "Models" },
    { to: "/hosts", label: "Machines" },
  ],
  hosted: [{ to: "/requests", label: "Requests" }],
};

/** One card per route, so local, hosted and account traffic are never lumped together. */
function RouteSummary({ status, records }: { status: Status; records: RequestRecord[] }) {
  const totals = new Map<RouteKind, { requests: number; models: number; input: number; output: number; last: string | null }>();
  for (const route of ROUTE_ORDER) totals.set(route, { requests: 0, models: 0, input: 0, output: 0, last: null });
  for (const record of records) {
    const total = totals.get(routeOf(record, status.backends))!;
    total.requests++;
    if (record.model) total.models++;
    total.input += record.input_tokens;
    total.output += record.output_tokens;
    if (!total.last || record.timestamp > total.last) total.last = record.timestamp;
  }
  const inFlight = (route: RouteKind) => status.current_requests.filter((item) => routeOf(item, status.backends) === route).length;
  const configured = (route: RouteKind) =>
    route === "account"
      ? Boolean(status.intercept?.running)
      : Object.values(status.backends).some((backend) => (backend.type === "anthropic" ? "hosted" : "ollama") === route);
  return (
    <section className="route-cards" aria-label="Traffic by route">
      {ROUTE_ORDER.map((route) => {
        const total = totals.get(route)!;
        const live = inFlight(route);
        return (
          <article key={route} className={`route-card route-${route}`}>
            <h2>
              <span className={`route-dot route-${route}`} aria-hidden="true" />
              {ROUTES[route].label}
              {live > 0 && <span className="pill live">{live} streaming</span>}
            </h2>
            <p className="subtle">{ROUTES[route].description}</p>
            {total.requests ? (
              <dl>
                <div>
                  <dt>Requests</dt>
                  <dd>
                    {formatNumber(total.requests)}
                    {route === "account" && <small> {formatNumber(total.models)} to models</small>}
                  </dd>
                </div>
                <div>
                  <dt>Tokens</dt>
                  <dd>
                    {formatCompact(total.input)} in, {formatCompact(total.output)} out
                  </dd>
                </div>
                <div>
                  <dt>Last used</dt>
                  <dd>{formatRelative(total.last)}</dd>
                </div>
              </dl>
            ) : (
              <p className="subtle">
                {configured(route)
                  ? "Not used yet."
                  : route === "account"
                    ? "The intercept listener isn't running."
                    : `No ${route === "ollama" ? "Ollama" : "hosted"} backend configured.`}
              </p>
            )}
            <p className="row">
              {configured(route) ? (
                <Link to="/agents">Start an agent</Link>
              ) : (
                <Link to="/settings">{route === "account" ? "Check settings" : "Add a backend"}</Link>
              )}
              {ROUTE_LINKS[route].map((link) => (
                <Link key={link.to} to={link.to}>
                  {link.label}
                </Link>
              ))}
            </p>
          </article>
        );
      })}
    </section>
  );
}

export function Offline({ message }: { message: string }) {
  return (
    <div className="page">
      <header className="page-head">
        <h1>Can't reach the proxy</h1>
        <p className="lede">
          Start it with <code>agent-proxy</code> and this page reconnects on its own. ({message})
        </p>
      </header>
    </div>
  );
}
