import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, type ServerLive, type ServerStats } from "../api";
import { LineChart, type Sample } from "../components/LineChart";
import { Meter } from "../components/Meter";
import { PartTag } from "../components/PartTag";
import { formatBytes, formatNumber } from "../format";
import { useTicker } from "../live";
import { ceiling, formatByteRate, span, useRates } from "../rates";

const POLL_MS = 2000;
const WINDOW_SECONDS = 300;
const GB = 1024 ** 3;
const percent = (value: number) => `${Math.round(value)}%`;
const gigabytes = (value: number) => `${value.toFixed(1)} GB`;
const gigabytesAxis = (value: number) => (value === 0 ? "0 GB" : `${value.toFixed(value >= 10 ? 0 : 1)} GB`);
const perSecond = (value: number) => `${value >= 10 ? Math.round(value) : value.toFixed(1)}/s`;
const perMinute = (value: number) => `${formatNumber(Math.round(value))}/min`;

/** What each kind of open WebSocket is, in the order shown. */
const SOCKET_LABELS: [string, string][] = [
  ["machines", "Machines"],
  ["model_tunnels", "Tunnels to model servers"],
  ["agent_tunnels", "Account agents' HTTPS"],
  ["terminal_hosts", "Shared terminals"],
  ["terminal_viewers", "Terminal viewers"],
  ["links", "Linked servers"],
  ["other", "Other"],
];

/** The last five minutes of CPU and memory, kept only while the page is open (the server saves nothing). */
function useHistory(sample: ServerStats | undefined) {
  const history = useRef<{ cpu: Sample[]; memory: Sample[]; process: Sample[]; inFlight: Sample[]; sockets: Sample[] }>({ cpu: [], memory: [], process: [], inFlight: [], sockets: [] });
  const [, setVersion] = useState(0);
  const last = useRef(0);
  useEffect(() => {
    if (!sample || sample.timestamp === last.current) return;
    last.current = sample.timestamp;
    const cutoff = sample.timestamp - WINDOW_SECONDS;
    const push = (series: Sample[], v: number) => {
      series.push({ t: sample.timestamp, v });
      while (series.length && series[0].t < cutoff) series.shift();
    };
    push(history.current.cpu, sample.cpu.percent);
    push(history.current.memory, sample.memory.used / GB);
    // psutil gives the process's CPU as a share of one core; the chart is of the whole computer.
    push(history.current.process, sample.process.cpu_percent / Math.max(1, sample.cpu.count));
    push(history.current.inFlight, sample.live.requests.in_flight);
    push(history.current.sockets, sample.live.connections.websockets + sample.live.connections.live_streams);
    setVersion((value) => value + 1);
  }, [sample]);
  return history.current;
}

const since = (seconds: number) => span(Date.now() / 1000 - seconds);

/** Administration › Server stats: what the server is doing now, how hard its computer is working, and how full its disks are. */
export function ServerStatsPage() {
  const stats = useQuery({
    queryKey: ["server-stats"],
    queryFn: api.serverStats,
    refetchInterval: (query) => (query.state.error instanceof ApiError && query.state.error.status === 403 ? false : POLL_MS),
    retry: false,
    gcTime: 0,
  });
  const history = useHistory(stats.data);
  const sample = stats.data;
  const totals = sample?.live.requests.totals;
  const rates = useRates(
    sample?.timestamp,
    sample
      ? {
          received: sample.network?.bytes_recv,
          sent: sample.network?.bytes_sent,
          requests: totals ? totals.ok + totals.failed + totals.dropped : null,
        }
      : null,
    WINDOW_SECONDS,
  );
  useTicker(true, 1000);
  const forbidden = stats.error instanceof ApiError && stats.error.status === 403;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Server stats</h1>
        <p className="lede">
          What the server is doing now, how hard its computer is working, and how full its disks are. Sampled every {POLL_MS / 1000}{" "}
          seconds while this page is open; nothing is saved.
        </p>
      </header>
      {stats.isPending && <p className="empty">Asking the server…</p>}
      {forbidden && <p className="empty">Only a site admin can see the server's stats.</p>}
      {stats.isError && !forbidden && <p className="bad">Couldn't read the server's stats: {stats.error.message}</p>}
      {sample && (
        <>
          <LiveNow live={sample.live} />
          <section className="section">
            <div className="section-head">
              <h2>Traffic</h2>
            </div>
            <div className="metric-grid-layout">
              <LineChart
                label="Requests"
                samples={rates.requests ?? []}
                max={ceiling(rates.requests ?? [], 5)}
                format={perSecond}
                windowSeconds={WINDOW_SECONDS}
                detail={`${formatNumber(sample.live.requests.totals.ok + sample.live.requests.totals.failed + sample.live.requests.totals.dropped)} since it started`}
              />
              <LineChart
                label="Requests in flight"
                samples={history.inFlight}
                max={ceiling(history.inFlight, 4)}
                format={(value) => formatNumber(Math.round(value))}
                windowSeconds={WINDOW_SECONDS}
                detail="model calls being answered"
              />
              <LineChart
                label="Open connections"
                samples={history.sockets}
                max={ceiling(history.sockets, 4)}
                format={(value) => formatNumber(Math.round(value))}
                windowSeconds={WINDOW_SECONDS}
                detail="WebSockets and live streams"
              />
              <LineChart
                label="Requests, last hour"
                samples={sample.live.requests.per_minute.map((minute) => ({ t: minute.minute, v: minute.ok + minute.failed + minute.dropped }))}
                max={Math.max(10, ...sample.live.requests.per_minute.map((minute) => (minute.ok + minute.failed + minute.dropped) * 1.2))}
                format={perMinute}
                windowSeconds={3600}
                detail={`${formatNumber(sample.live.requests.per_minute.reduce((total, minute) => total + minute.failed + minute.dropped, 0))} failed or dropped`}
              />
              {sample.network && (
                <>
                  <LineChart
                    label="Network received"
                    samples={rates.received ?? []}
                    max={ceiling(rates.received ?? [], 64 * 1024)}
                    format={formatByteRate}
                    windowSeconds={WINDOW_SECONDS}
                    detail={`${formatBytes(sample.network.bytes_recv)} in total`}
                  />
                  <LineChart
                    label="Network sent"
                    samples={rates.sent ?? []}
                    max={ceiling(rates.sent ?? [], 64 * 1024)}
                    format={formatByteRate}
                    windowSeconds={WINDOW_SECONDS}
                    detail={`${formatBytes(sample.network.bytes_sent)} in total`}
                  />
                </>
              )}
            </div>
            <p className="subtle">
              Requests are everything the server answered, the console's own calls included. The network is the whole computer's since it started
              {sample.network ? `, over ${sample.network.interfaces} ${sample.network.interfaces === 1 ? "interface" : "interfaces"} (loopback left out)` : ""}.
            </p>
          </section>
          <Technical sample={sample} />
          <section className="section">
            <div className="section-head">
              <h2>CPU and memory</h2>
            </div>
            <div className="metric-grid-layout">
              <LineChart
                label="CPU"
                samples={history.cpu}
                max={100}
                format={percent}
                windowSeconds={WINDOW_SECONDS}
                detail={[
                  `${sample.cpu.count} cores`,
                  sample.cpu.load_average ? `load ${sample.cpu.load_average.map((value) => value.toFixed(2)).join(", ")}` : null,
                ]
                  .filter(Boolean)
                  .join(", ")}
              />
              <LineChart
                label="Memory"
                samples={history.memory}
                max={sample.memory.total / GB}
                format={gigabytes}
                formatAxis={gigabytesAxis}
                windowSeconds={WINDOW_SECONDS}
                detail={`of ${formatBytes(sample.memory.total)}, ${formatBytes(sample.memory.available)} available`}
              />
              <LineChart
                label="The server's own CPU"
                samples={history.process}
                max={100}
                format={percent}
                windowSeconds={WINDOW_SECONDS}
                detail={`of the whole computer, ${sample.process.threads} threads`}
              />
            </div>
            <div className="meter-grid">
              <Meter
                label="The server's own memory"
                value={sample.process.rss}
                max={sample.memory.total}
                text={formatBytes(sample.process.rss)}
                detail={`of ${formatBytes(sample.memory.total)}`}
              />
              {sample.swap.total > 0 && (
                <Meter
                  label="Swap"
                  value={sample.swap.used}
                  max={sample.swap.total}
                  text={`${formatBytes(sample.swap.used)} of ${formatBytes(sample.swap.total)}`}
                />
              )}
            </div>
          </section>
          <section className="section">
            <div className="section-head">
              <h2>Disks</h2>
            </div>
            <div className="meter-grid">
              {sample.disks.map((disk) => (
                <Meter
                  key={disk.mountpoint}
                  label={disk.mountpoint}
                  value={disk.used}
                  max={disk.total}
                  text={`${formatBytes(disk.used)} of ${formatBytes(disk.total)}`}
                  detail={`${formatBytes(disk.free)} free. Holds: ${disk.folders.join(", ").toLowerCase()}.`}
                />
              ))}
            </div>
            {!sample.disks.length && <p className="empty">Couldn't find the disks the server's folders are on.</p>}
          </section>
          <section className="section">
            <div className="section-head">
              <h2>What the server keeps</h2>
            </div>
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Where</th>
                    <th scope="col" className="num">Size</th>
                    <th scope="col">Making room</th>
                  </tr>
                </thead>
                <tbody>
                  {sample.database && (
                    <tr>
                      <td>
                        <strong>The database</strong>
                        <small>
                          {sample.database.kind === "postgresql" ? "PostgreSQL" : "SQLite"}: settings, accounts, machines, and every session's
                          requests, payloads and terminals
                        </small>
                      </td>
                      <td className="num nowrap">
                        {sample.database.bytes != null ? formatBytes(sample.database.bytes) : sample.folders_measuring || sample.folders_measured_at == null ? "Measuring…" : "Unknown"}
                      </td>
                      <td>
                        <Link to="/sessions">Delete sessions</Link>, or keep payloads and terminals for fewer days in{" "}
                        <Link to="/settings">Server settings</Link>.
                      </td>
                    </tr>
                  )}
                  {sample.folders.map((folder, index) => (
                    <tr key={folder.path}>
                      <td>
                        <strong>{folder.name}</strong>
                        <small className="mono">{folder.path}</small>
                      </td>
                      <td className="num nowrap">
                        {folder.bytes != null ? formatBytes(folder.bytes) : sample.folders_measuring || sample.folders_measured_at == null ? "Measuring…" : "Not there"}
                      </td>
                      <td>
                        {index === 0 ? (
                          <>The server's own log. Without PostgreSQL, the database file (above) is in this folder too.</>
                        ) : (
                          <>Old builds (folders named like 1.2.0+abc1234) can be deleted by hand; the server hands out only its own version's.</>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {sample.folders_measured_at != null && (
              <p className="subtle">
                Sizes measured {Date.now() / 1000 - sample.folders_measured_at < 60 ? "just now" : `${since(sample.folders_measured_at)} ago`}; they're measured again every
                five minutes.
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/** The counts that matter at a glance: what's in flight, who's connected, how much is open. */
function LiveNow({ live }: { live: ServerLive }) {
  return (
    <dl className="stat-cards" aria-label="Right now">
      <div>
        <dt>Requests in flight</dt>
        <dd>
          {formatNumber(live.requests.in_flight)}{" "}
          {live.sessions.streaming > 0 && (
            <small>
              in {live.sessions.streaming} {live.sessions.streaming === 1 ? "session" : "sessions"}
            </small>
          )}
        </dd>
      </div>
      <div>
        <dt>Open connections</dt>
        <dd>
          {formatNumber(live.connections.websockets + live.connections.live_streams)} <small>{live.connections.websockets} WebSockets</small>
        </dd>
      </div>
      <div>
        <dt>Open sessions</dt>
        <dd>
          {formatNumber(live.sessions.open)} <small>of {formatNumber(live.sessions.total)}</small>
        </dd>
      </div>
      <div>
        <dt>Machines connected</dt>
        <dd>
          {formatNumber(live.fleet.connected)} <small>of {formatNumber(live.fleet.machines)}</small>
        </dd>
      </div>
      <div>
        <dt>Shared terminals</dt>
        <dd>
          {formatNumber(live.terminals.shared)}{" "}
          <small>
            {live.terminals.viewers} {live.terminals.viewers === 1 ? "viewer" : "viewers"}
          </small>
        </dd>
      </div>
      <div>
        <dt>Server processes</dt>
        <dd>{formatNumber(live.nodes.length)}</dd>
      </div>
    </dl>
  );
}

/** Every count behind the cards, for finding out where connections and load come from. */
function Technical({ sample }: { sample: ServerStats }) {
  const live = sample.live;
  const sockets = SOCKET_LABELS.filter(([kind]) => live.connections.by_kind[kind]);
  const facts: [string, string][] = [
    ["Server running for", `${since(sample.process.started_at)} (process ${sample.process.pid})`],
    ["Since its last restart", since(live.started_at)],
    ["Computer", `${sample.hostname}, ${sample.platform}`],
    ["Computer up for", since(sample.boot_time)],
    ["Answered", `${formatNumber(live.requests.totals.ok)} ok, ${formatNumber(live.requests.totals.failed)} failed, ${formatNumber(live.requests.totals.dropped)} dropped`],
    ["Live streams", `${formatNumber(live.connections.live_streams)} (the console's Live and Traffic pages)`],
    ["Tunnels to model servers", `${formatNumber(live.fleet.tunnels)} open, ${formatNumber(live.fleet.tunnel_listeners)} ${live.fleet.tunnel_listeners === 1 ? "listener" : "listeners"}`],
    ["Account agents' HTTPS tunnels", formatNumber(live.intercept_tunnels)],
    ["Linked servers", `${formatNumber(live.links.connected)} connected of ${formatNumber(live.links.total)}`],
    ["Waiting to be written", `${formatNumber(live.write_queue)} ${live.write_queue === 1 ? "record" : "records"}`],
    ["The server's sockets", sample.process.connections == null ? "Not available here" : formatNumber(sample.process.connections)],
    ["The server's open files", sample.process.open_files == null ? "Not available here" : formatNumber(sample.process.open_files)],
    ...(sample.network
      ? ([
          ["Network packets", `${formatNumber(sample.network.packets_recv)} in, ${formatNumber(sample.network.packets_sent)} out`],
          ["Network errors", formatNumber(sample.network.errors)],
        ] as [string, string][])
      : []),
  ];
  return (
    <section className="section">
      <div className="section-head">
        <h2>Technical details</h2>
      </div>
      <dl className="fact-list">
        {facts.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <div className="table-scroll">
        <table className="table">
          <caption className="visually-hidden">Open WebSockets by kind</caption>
          <thead>
            <tr>
              <th scope="col">Open WebSockets</th>
              <th scope="col" className="num">Count</th>
            </tr>
          </thead>
          <tbody>
            {sockets.map(([kind, label]) => (
              <tr key={kind}>
                <td>{label}</td>
                <td className="num">{formatNumber(live.connections.by_kind[kind])}</td>
              </tr>
            ))}
            {!sockets.length && (
              <tr>
                <td colSpan={2} className="subtle">
                  None open.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="subtle">
        Who holds each one is on the <Link to="/access-log">Access log</Link>.
      </p>
      {live.nodes.length > 1 && (
        <div className="table-scroll">
          <table className="table">
            <caption className="visually-hidden">Server processes</caption>
            <thead>
              <tr>
                <th scope="col">Server process</th>
                <th scope="col">Last heard from</th>
                <th scope="col" className="num">Live streams</th>
              </tr>
            </thead>
            <tbody>
              {live.nodes.map((node) => (
                <tr key={node.id}>
                  <td className="mono">
                    {node.id} {node.this && <span className="pill">answered this</span>}
                  </td>
                  <td>{node.this || node.seen_at == null ? "now" : `${Math.max(0, Math.round(Date.now() / 1000 - node.seen_at))} s ago`}</td>
                  <td className="num">{node.live_streams ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {live.nodes.length > 1 && <p className="subtle">The counts on this page are from the process that answered; each process keeps its own.</p>}
    </section>
  );
}
