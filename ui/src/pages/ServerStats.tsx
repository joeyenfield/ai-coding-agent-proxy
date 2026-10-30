import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, type ServerStats } from "../api";
import { LineChart, type Sample } from "../components/LineChart";
import { Meter } from "../components/Meter";
import { PartTag } from "../components/PartTag";
import { formatBytes } from "../format";
import { useTicker } from "../live";

const POLL_MS = 2000;
const WINDOW_SECONDS = 300;
const GB = 1024 ** 3;
const percent = (value: number) => `${Math.round(value)}%`;
const gigabytes = (value: number) => `${value.toFixed(1)} GB`;
const gigabytesAxis = (value: number) => (value === 0 ? "0 GB" : `${value.toFixed(value >= 10 ? 0 : 1)} GB`);

/** The last five minutes of CPU and memory, kept only while the page is open (the server saves nothing). */
function useHistory(sample: ServerStats | undefined) {
  const history = useRef<{ cpu: Sample[]; memory: Sample[]; process: Sample[] }>({ cpu: [], memory: [], process: [] });
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
    setVersion((value) => value + 1);
  }, [sample]);
  return history.current;
}

/** Days, hours and minutes since a Unix time. */
function since(seconds: number) {
  const elapsed = Math.max(0, Date.now() / 1000 - seconds);
  const days = Math.floor(elapsed / 86400);
  const hours = Math.floor((elapsed % 86400) / 3600);
  const minutes = Math.floor((elapsed % 3600) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

/** Server › Server stats: how hard the computer running agent-server is working, and how full its disks are. */
export function ServerStatsPage() {
  const stats = useQuery({
    queryKey: ["server-stats"],
    queryFn: api.serverStats,
    refetchInterval: (query) => (query.state.error instanceof ApiError && query.state.error.status === 403 ? false : POLL_MS),
    retry: false,
    gcTime: 0,
  });
  const history = useHistory(stats.data);
  useTicker(true, 1000);
  const sample = stats.data;
  const forbidden = stats.error instanceof ApiError && stats.error.status === 403;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Server stats</h1>
        <p className="lede">
          How hard the computer running the server is working, and how full its disks are. Sampled every {POLL_MS / 1000} seconds while
          this page is open; nothing is saved. Each machine's own load is on its page.
        </p>
      </header>
      {stats.isPending && <p className="empty">Asking the server…</p>}
      {forbidden && <p className="empty">Only an admin can see the server's stats.</p>}
      {stats.isError && !forbidden && <p className="bad">Couldn't read the server's stats: {stats.error.message}</p>}
      {sample && (
        <>
          <section className="section">
            <dl className="fact-list">
              <div>
                <dt>Computer</dt>
                <dd>{sample.hostname}</dd>
              </div>
              <div>
                <dt>System</dt>
                <dd>{sample.platform}</dd>
              </div>
              <div>
                <dt>Computer up for</dt>
                <dd>{since(sample.boot_time)}</dd>
              </div>
              <div>
                <dt>Server running for</dt>
                <dd>
                  {since(sample.process.started_at)} <small>(process {sample.process.pid})</small>
                </dd>
              </div>
            </dl>
          </section>
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
