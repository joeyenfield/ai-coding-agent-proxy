import { Link } from "react-router-dom";
import { requestKey } from "../api";
import { RoutingMap } from "../components/RoutingMap";
import { ThroughputChart } from "../components/ThroughputChart";
import { formatBytes, formatCompact, formatDuration, formatNumber, formatRelative } from "../format";
import { useSessions, useStatus } from "../hooks";

export function OverviewPage() {
  const status = useStatus();
  const sessions = useSessions();
  if (status.isError) return <Offline message={status.error.message} />;
  if (!status.data) return <p className="empty">Connecting to the proxy…</p>;
  const data = status.data;
  const live = data.current_requests;
  return (
    <div className="page">
      <header className="page-head">
        <h1>{live.length ? `${live.length} ${live.length === 1 ? "request" : "requests"} streaming` : "All quiet"}</h1>
        <p className="lede">
          Every agent request passes through this proxy on its way to Ollama.
        </p>
      </header>

      <RoutingMap status={data} sessions={sessions.data ?? []} />

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
          <h2>Streaming now</h2>
          <ul className="live-list">
            {live.map((item) => (
              <li key={requestKey(item)}>
                <span className="live-mark" aria-hidden="true" />
                <strong>{item.client}</strong>
                <span>{item.model}</span>
                <span className="subtle">{item.backend}</span>
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
        <ThroughputChart records={data.recent_requests} />
      </section>

      <section className="section">
        <div className="section-head">
          <h2>Latest requests</h2>
          <Link to="/requests">Open request history</Link>
        </div>
        {data.recent_requests.length ? (
          <table className="table compact">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Agent</th>
                <th scope="col" className="num">First token</th>
                <th scope="col" className="num">Tokens/s</th>
                <th scope="col" className="num">When</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_requests.slice(0, 8).map((record) => (
                <tr key={requestKey(record)}>
                  <td>
                    <Link to={`/requests?request=${encodeURIComponent(requestKey(record))}`}>{record.model || "Unknown model"}</Link>
                  </td>
                  <td>{record.client}</td>
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
