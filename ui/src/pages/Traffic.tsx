import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { isFailure, outcomeLabel, type RequestRecord } from "../api";
import { DetailBack, useDetailView } from "../components/DetailView";
import { Inspector } from "../components/Inspector";
import { LiveDetails } from "../components/LiveDetails";
import { RouteBadge } from "../components/RouteBadge";
import { formatBytes, formatDuration, formatNumber, shortId } from "../format";
import { useRequests, useSessions, useStatus } from "../hooks";
import { useLive, useTicker } from "../live";
import { CATEGORY_LABELS, CATEGORY_ORDER, groupRepeats, mergeRows, type Category, type Row } from "../traffic";
import { PartTag } from "../components/PartTag";

const ROW_LIMIT = 300;

export function TrafficPage() {
  const status = useStatus();
  const requests = useRequests();
  const sessions = useSessions();
  const live = useLive();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  useTicker(live.active.length > 0, 1000);
  const category = (params.get("type") ?? "") as Category | "";
  const failedOnly = params.get("failed") === "1";
  const grouped = params.get("group") !== "0";
  const sessionFilter = params.get("session") ?? "";
  const selectedKey = params.get("request");

  const rows = useMemo(() => mergeRows(requests.data ?? [], [...live.recent, ...live.active]), [requests.data, live.active, live.recent]);

  // Session, search and failure filters; the category chips count within these.
  const scoped = useMemo(() => {
    const text = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (sessionFilter && row.session_id !== sessionFilter) return false;
      if (failedOnly && !(row.record && isFailure(row.record))) return false;
      return (
        !text ||
        [row.service, CATEGORY_LABELS[row.category], row.host, row.path, row.client, row.model, row.method, row.request_id].some((value) =>
          value.toLowerCase().includes(text),
        )
      );
    });
  }, [rows, query, sessionFilter, failedOnly]);
  const counts = useMemo(() => {
    const result = new Map<Category, number>();
    for (const row of scoped) result.set(row.category, (result.get(row.category) ?? 0) + 1);
    return result;
  }, [scoped]);
  const matches = useMemo(() => {
    const filtered = category ? scoped.filter((row) => row.category === category) : scoped;
    return grouped ? groupRepeats(filtered) : filtered;
  }, [scoped, category, grouped]);
  const selected = rows.find((row) => row.key === selectedKey);
  const detail = useDetailView(Boolean(selected));
  const intercept = status.data?.intercept;
  const shown = matches.reduce((total, row) => total + 1 + (row.repeats?.length ?? 0), 0);
  const services = new Set(matches.map((row) => row.service)).size;
  // Over every request the filters match, folded repeats included, not just the rows drawn.
  const totals = useMemo(() => {
    const sum = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, estimated: false };
    for (const row of matches) {
      for (const record of [row.record, ...(row.repeats ?? []).map((repeat) => repeat.record)]) {
        if (!record) continue;
        sum.input += record.input_tokens;
        sum.output += record.output_tokens;
        sum.cacheRead += record.cache_read_tokens ?? 0;
        sum.cacheWrite += record.cache_creation_tokens ?? 0;
        sum.estimated ||= Boolean(record.tokens_estimated);
      }
    }
    return sum;
  }, [matches]);
  const anyTokens = totals.input + totals.output + totals.cacheRead + totals.cacheWrite > 0;

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  return (
    <div className={`page wide ${selected ? "detail-open" : ""}`}>
      <header className="page-head">
        <PartTag />
        <h1>Traffic</h1>
        <p className="lede">
          Every request that passes through the server: model calls, and everything account agents send to their own
          services, including sign-in, telemetry and MCP calls.
          {!live.connected && <span className="bad"> Reconnecting to the server…</span>}
        </p>
        {intercept && !intercept.running && <p className="bad">HTTPS capture is off on this server: {intercept.error}</p>}
      </header>
      <dl className="stat-cards" aria-label="Traffic shown">
        <div>
          <dt>Requests</dt>
          <dd>
            {formatNumber(shown)} {live.active.length > 0 && <small>{live.active.length} in flight</small>}
          </dd>
        </div>
        <div>
          <dt>Services</dt>
          <dd>{formatNumber(services)}</dd>
        </div>
        <div>
          <dt>Tokens in</dt>
          <dd title={totals.estimated ? "Partly estimated: a stream ended before the provider reported counts" : undefined}>
            {anyTokens ? `${totals.estimated ? "≈ " : ""}${formatNumber(totals.input)}` : "–"}
          </dd>
        </div>
        <div>
          <dt>Tokens out</dt>
          <dd>{anyTokens ? `${totals.estimated ? "≈ " : ""}${formatNumber(totals.output)}` : "–"}</dd>
        </div>
        <div>
          <dt>Prompt cache</dt>
          <dd title={`${formatNumber(totals.cacheRead)} tokens read from the prompt cache, ${formatNumber(totals.cacheWrite)} written to it`}>
            {totals.cacheRead || totals.cacheWrite ? (
              <>
                {formatNumber(totals.cacheRead)} <small>read</small>
              </>
            ) : (
              "–"
            )}
          </dd>
        </div>
      </dl>
      <div className="split traffic-split">
        <div className="split-main">
          <div className="filters">
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search service, host, path, agent or model"
              aria-label="Search traffic"
            />
            <select value={sessionFilter} onChange={(event) => setParam("session", event.target.value)} aria-label="Session">
              <option value="">All sessions</option>
              {sessions.data?.map((session) => (
                <option key={session.session_id} value={session.session_id}>
                  {session.client} {shortId(session.session_id)}
                </option>
              ))}
            </select>
          </div>
          <div className="chip-bar" role="group" aria-label="Request type">
            <button type="button" aria-pressed={!category} onClick={() => setParam("type", null)}>
              All <span className="count">{formatNumber(scoped.length)}</span>
            </button>
            {CATEGORY_ORDER.filter((name) => counts.get(name)).map((name) => (
              <button
                key={name}
                type="button"
                className={`category-${name}`}
                aria-pressed={category === name}
                onClick={() => setParam("type", category === name ? null : name)}
              >
                {CATEGORY_LABELS[name]} <span className="count">{formatNumber(counts.get(name))}</span>
              </button>
            ))}
            <span className="chip-gap" aria-hidden="true" />
            <button type="button" aria-pressed={failedOnly} onClick={() => setParam("failed", failedOnly ? null : "1")}>
              Failed only
            </button>
            <button
              type="button"
              aria-pressed={grouped}
              title="Fold identical requests in a row into one"
              onClick={() => setParam("group", grouped ? "0" : null)}
            >
              Group repeats
            </button>
          </div>
          {requests.isError ? (
            <p className="empty bad">{requests.error.message}</p>
          ) : !matches.length ? (
            <p className="empty">{rows.length ? "No traffic matches these filters." : "No traffic has been recorded yet."}</p>
          ) : (
            <>
              <ul className="traffic-list" aria-label="Requests">
                {matches.slice(0, ROW_LIMIT).map((row) => (
                  <TrafficRow key={row.key} row={row} selected={row.key === selectedKey} onSelect={() => detail.select(row.key)} />
                ))}
              </ul>
              {matches.length > ROW_LIMIT && <p className="subtle">Showing the newest {ROW_LIMIT} rows.</p>}
            </>
          )}
        </div>
        <div className="split-side">
          {selected ? (
            <>
              <DetailBack label="Back to traffic" onBack={detail.close} />
              <Details key={selected.key} row={selected} />
            </>
          ) : (
            <div className="inspector placeholder">
              <p>Select a request to see its headers, body and timing.</p>
              {intercept?.running && (
                <p className="subtle">
                  Account agents (<code>agent claude-account .</code>, <code>copilot-account</code>, <code>codex-account</code>) are
                  captured through the agent-proxy on their machine, <span className="mono">{intercept.url}</span>. Start one from{" "}
                  <Link to="/agents">Start an agent</Link>.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Details({ row }: { row: Row }) {
  const sessions = useSessions();
  if (!row.record) {
    return (
      <section className="inspector" aria-label="Request details">
        <header className="inspector-head">
          <div>
            <h2>{row.model || `${CATEGORY_LABELS[row.category]}: ${row.service}`}</h2>
            <p className="mono subtle">
              {row.method} {row.host}
              {row.path}
            </p>
          </div>
          <span className="pill live">In flight</span>
        </header>
        <LiveDetails sessionId={row.session_id} requestId={row.request_id} done={false} />
      </section>
    );
  }
  return (
    <>
      <Inspector record={row.record} session={sessions.data?.find((session) => session.session_id === row.session_id)} />
      {!row.record.trace_available && row.live && (
        <section className="section">
          <h2>Still in memory</h2>
          <p className="subtle">This session doesn't save bodies, but the proxy still holds this recent request.</p>
          <LiveDetails sessionId={row.session_id} requestId={row.request_id} done />
        </section>
      )}
    </>
  );
}

/** One request: what it was and where it went, then how it ended, how long it took and what it cost. */
function TrafficRow({ row, selected, onSelect }: { row: Row; selected: boolean; onSelect: () => void }) {
  const record = row.record;
  const failed = record ? isFailure(record) : false;
  const elapsed = record ? record.total_ms : row.live ? Date.now() - row.live.started_at * 1000 : null;
  const estimated = record?.tokens_estimated ? "≈ " : "";
  const cached = (record?.cache_read_tokens ?? 0) + (record?.cache_creation_tokens ?? 0);
  return (
    <li>
      <button type="button" className={`traffic-item ${failed ? "is-bad" : ""}`} aria-pressed={selected} onClick={onSelect}>
        <span className="traffic-item-line">
          <span className={`pill category category-${row.category}`}>{CATEGORY_LABELS[row.category]}</span>
          <strong title={row.host}>{row.service}</strong>
          {row.model && <span className="pill accent">{row.model}</span>}
          {row.repeats?.length ? (
            <span className="pill" title={`${row.repeats.length + 1} identical requests in a row; showing the newest`}>
              ×{row.repeats.length + 1}
            </span>
          ) : null}
          <span className="traffic-item-end">
            {record ? (
              <span className={`pill ${failed ? "bad" : "good"}`} title={outcomeLabel(record) ?? undefined}>
                {record.status}
              </span>
            ) : (
              <span className="pill live">pending</span>
            )}
          </span>
        </span>
        <span className="traffic-item-line">
          <span className="mono traffic-path" title={`${row.method} ${row.host}${row.path}`}>
            <span className="method">{row.method}</span> {row.path}
          </span>
          <span className="traffic-item-end">{formatDuration(elapsed)}</span>
        </span>
        <span className="traffic-item-line traffic-item-meta">
          <RouteBadge item={{ backend: row.record?.backend ?? row.live?.backend ?? "", client: row.client, kind: row.kind as RequestRecord["kind"] }} />
          <span>
            {row.client} #{row.request_id}
            {row.kind === "tunnel" ? ", not decrypted" : row.kind === "upgrade" ? ", WebSocket" : ""}
          </span>
          <span>{new Date(row.started).toLocaleTimeString()}</span>
          <span className="traffic-item-end">
            {record && (record.input_tokens || record.output_tokens) ? (
              <span
                title={`${estimated ? "Estimated: the stream ended before the provider reported counts. " : ""}${formatNumber(record.input_tokens)} tokens in, ${formatNumber(record.output_tokens)} out${cached ? `, ${formatNumber(record.cache_read_tokens ?? 0)} read from and ${formatNumber(record.cache_creation_tokens ?? 0)} written to the prompt cache` : ""}`}
              >
                {estimated}
                {formatNumber(record.input_tokens)} in · {formatNumber(record.output_tokens)} out
                {cached ? ` · ${formatNumber(record.cache_read_tokens ?? 0)} cached` : ""}
              </span>
            ) : record ? (
              <span title={`${formatBytes(record.request_bytes)} sent, ${formatBytes(record.response_bytes)} received`}>
                ↑ {formatBytes(record.request_bytes)} ↓ {formatBytes(record.response_bytes)}
              </span>
            ) : null}
          </span>
        </span>
      </button>
    </li>
  );
}
