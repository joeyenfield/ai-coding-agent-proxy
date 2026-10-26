import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { isModelCall, requestKey, type RequestRecord, type Status } from "../api";
import { PartNumber } from "../components/PartTag";
import { providerState, StateDot } from "../components/ProviderState";
import { RouteBadge } from "../components/RouteBadge";
import { RoutingMap } from "../components/RoutingMap";
import { ThroughputChart } from "../components/ThroughputChart";
import { formatBytes, formatCompact, formatDuration, formatNumber, formatRelative } from "../format";
import { useAgents, useMachines, useModels, useRequests, useSessions, useStatus } from "../hooks";
import { isGroup, PARTS, type NavLinkItem, type Part } from "../navigation";
import { providerLabel, ROUTE_ORDER, ROUTES, routeOf, type RouteKind } from "../routes";

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
          You start an agent, its traffic passes through this server, the hub, and a machine answers it: the Ollama, Strata or
          Docker server one of your machines runs, or a container it runs for you. Hosted APIs and agents' own accounts are the
          only answers that don't come from a machine.
        </p>
      </header>

      <RoutingMap status={data} sessions={sessions.data ?? []} />

      <PartCards status={data} />

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
          <h2>Traffic by route</h2>
          <p className="subtle">How each request reached its model</p>
        </div>
        <RouteSummary status={data} records={requests.data ?? data.recent_requests} />
      </section>

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
            <p>No requests yet. Start an agent to see its traffic here.</p>
            <Link className="button primary" to="/agents">
              Choose an agent
            </Link>
          </div>
        )}
      </section>
    </div>
  );
}

/** The parts in the order a request flows (agent, server, machine), each with its key numbers and pages. */
function PartCards({ status }: { status: Status }) {
  const agents = useAgents();
  const models = useModels();
  const machines = useMachines();
  const [agentsPart, serverPart, machinesPart] = PARTS;
  const fleet = machines.data?.machines ?? [];
  const installed = (agents.data ?? []).filter((agent) => agent.installed).length;
  const live = status.current_requests.length;
  return (
    <section className="part-cards" aria-label="The three parts">
      <PartCard
        part={agentsPart}
        action={
          <>
            <Link className="button primary" to="/agents">
              Start an agent
            </Link>
            <Link to="/terminals">Running agents</Link>
          </>
        }
      >
        <dl>
          <div>
            <dt>Open sessions</dt>
            <dd>
              {formatNumber(status.active_sessions)}
              <small>of {formatNumber(status.total_sessions)} runs</small>
            </dd>
          </div>
          <div>
            <dt>Installed</dt>
            <dd>
              {agents.data ? formatNumber(installed) : "–"}
              <small>of {agents.data ? formatNumber(agents.data.length) : "–"} agents set up</small>
            </dd>
          </div>
        </dl>
      </PartCard>
      <PartCard
        part={serverPart}
        links={[
          { to: "/live", label: "Live" },
          { to: "/traffic", label: "Traffic" },
          { to: "/settings", label: "Server settings" },
        ]}
      >
        <dl>
          <div>
            <dt>Requests</dt>
            <dd>
              {formatNumber(status.total_requests)}
              <small className={live ? "warn" : undefined}>{live ? `${live} streaming now` : "none in flight"}</small>
            </dd>
          </div>
          <div>
            <dt>Tokens</dt>
            <dd>
              {formatCompact(status.total_input_tokens)}
              <small>read, {formatCompact(status.total_output_tokens)} written</small>
            </dd>
          </div>
        </dl>
      </PartCard>
      <PartCard
        part={machinesPart}
        links={[
          { to: "/machines", label: "Machines" },
          { to: "/machines#add", label: "Add a machine" },
        ]}
      >
        <dl>
          <div>
            <dt>Machines</dt>
            <dd>
              {machines.data ? formatNumber(fleet.filter((machine) => machine.connected).length) : "–"}
              <small>online of {formatNumber(fleet.length)}</small>
            </dd>
          </div>
        </dl>
        <ul className="provider-states">
          {Object.entries(status.backends).map(([name, backend]) => {
            const state = providerState(backend.type, models.data?.backends[name], models.isPending);
            return (
              <li key={name}>
                <StateDot tone={state.tone} />
                <strong>{name}</strong>
                <span className="subtle">
                  {providerLabel(backend.type, true)}
                  {backend.machine_name ? ` on ${backend.machine_name}` : backend.type === "anthropic" ? ", hosted" : ""}
                </span>
                <span className={`state-text ${state.tone}`}>{state.text}</span>
              </li>
            );
          })}
        </ul>
      </PartCard>
    </section>
  );
}

function PartCard({ part, children, action, links }: { part: Part; children: ReactNode; action?: ReactNode; links?: NavLinkItem[] }) {
  const pages = links ?? part.items.filter((item): item is NavLinkItem => !isGroup(item));
  return (
    <article className={`part-card part-${part.id}`}>
      <h2>
        <PartNumber value={part.number} />
        {part.label}
      </h2>
      <p className="subtle">{part.summary}</p>
      {children}
      <p className="row wrap part-card-links">
        {action ??
          pages.map((item) => (
            <Link key={item.to} to={item.to}>
              {item.label}
            </Link>
          ))}
      </p>
    </article>
  );
}

const ROUTE_LINKS: Record<RouteKind, { to: string; label: string }[]> = {
  account: [{ to: "/traffic", label: "All traffic" }],
  ollama: [{ to: "/machines", label: "Machines" }],
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
    <div className="route-cards">
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
                    ? "HTTPS capture is off on this server."
                    : route === "ollama"
                      ? "No machine supplies a local provider yet."
                      : "No hosted API added."}
              </p>
            )}
            <p className="row">
              {configured(route) ? (
                <Link to="/agents">Start an agent</Link>
              ) : (
                <Link to={route === "account" ? "/settings" : route === "ollama" ? "/machines#add" : "/settings"}>
                  {route === "account" ? "Check settings" : route === "ollama" ? "Add a machine" : "Add a hosted API"}
                </Link>
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
    </div>
  );
}

export function Offline({ message }: { message: string }) {
  return (
    <div className="page">
      <header className="page-head">
        <h1>Can't reach the proxy</h1>
        <p className="lede">
          Start it with <code>agent-server</code> and this page reconnects on its own. ({message})
        </p>
      </header>
    </div>
  );
}
