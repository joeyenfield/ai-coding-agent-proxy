import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { isFailure, outcomeLabel, type RequestRecord } from "../api";
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
    setParams(next, { replace: key !== "request" });
  };

  return (
    <div className="page wide">
      <header className="page-head">
        <PartTag />
        <h1>Traffic</h1>
        <p className="lede">
          Every request that passes through the proxy: model calls on proxy endpoints, and everything account agents send
          to their own services through their machine's agent-proxy, including sign-in, telemetry and MCP calls.
          {!live.connected && <span className="bad"> Reconnecting to the proxy…</span>}
        </p>
        {intercept && (
          <p className={`subtle ${intercept.running ? "" : "bad"}`}>
            {intercept.running ? (
              <>
                Account agents use the agent-proxy's HTTPS proxy, <span className="mono">{intercept.url}</span> on their
                machine. Start one from the{" "}
                <Link to="/agents">Agents</Link> page, for example <code>agent claude-account .</code>,{" "}
                <code>agent copilot-account .</code> or <code>agent codex-account .</code>
              </>
            ) : (
              <>HTTPS capture is off on this server: {intercept.error}</>
            )}
          </p>
        )}
      </header>
      <div className="split">
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
            <label className="check">
              <input type="checkbox" checked={failedOnly} onChange={(event) => setParam("failed", event.target.checked ? "1" : null)} />
              Failed only
            </label>
            <label className="check">
              <input type="checkbox" checked={grouped} onChange={(event) => setParam("group", event.target.checked ? null : "0")} />
              Group repeats
            </label>
          </div>
          <div className="category-bar" role="group" aria-label="Request type">
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
          </div>
          <p className="subtle">
            {formatNumber(shown)} requests to {formatNumber(services)} {services === 1 ? "service" : "services"}
            {live.active.length ? `, ${live.active.length} in flight` : ""}
            {matches.length > ROW_LIMIT ? `. Showing the newest ${ROW_LIMIT} rows.` : "."}
          </p>
          {requests.isError ? (
            <p className="empty bad">{requests.error.message}</p>
          ) : !matches.length ? (
            <p className="empty">{rows.length ? "No traffic matches these filters." : "No traffic has been recorded yet."}</p>
          ) : (
            <div className="table-scroll">
              <table className="table selectable compact traffic-table">
                <thead>
                  <tr>
                    <th scope="col">Request</th>
                    <th scope="col">Status and time</th>
                    <th scope="col" className="num tokens" title="Input tokens the provider reported (not counting cache reads on Anthropic)">In</th>
                    <th scope="col" className="num tokens" title="Output tokens">Out</th>
                    <th scope="col" className="num tokens" title="Prompt tokens served from the provider's cache">Cache<br />read</th>
                    <th scope="col" className="num tokens" title="Prompt tokens written to the provider's cache">Cache<br />write</th>
                  </tr>
                </thead>
                <tbody>
                  {matches.slice(0, ROW_LIMIT).map((row) => (
                    <TrafficRow key={row.key} row={row} selected={row.key === selectedKey} onSelect={() => setParam("request", row.key)} />
                  ))}
                </tbody>
                {anyTokens && (
                  <tfoot>
                    <tr className="traffic-totals">
                      <th scope="row" colSpan={2}>
                        Total of {formatNumber(shown)} {shown === 1 ? "request" : "requests"}
                      </th>
                      <TokenCell value={totals.input} estimated={totals.estimated} />
                      <TokenCell value={totals.output} estimated={totals.estimated} />
                      <TokenCell value={totals.cacheRead} />
                      <TokenCell value={totals.cacheWrite} />
                    </tr>
                  </tfoot>
                )}
              </table>
            </div>
          )}
        </div>
        <div className="split-side">
          {selected ? (
            <Details key={selected.key} row={selected} />
          ) : (
            <div className="inspector placeholder">Select a request to see its headers, body and timing.</div>
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

/** One token count; blank for requests that carry none (sign-in, telemetry, in flight). */
function TokenCell({ value, estimated }: { value?: number | null; estimated?: boolean }) {
  return (
    <td className="num nowrap tokens" title={estimated ? "Estimated: the stream ended before the provider reported counts" : undefined}>
      {value ? `${estimated ? "≈ " : ""}${formatNumber(value)}` : <span className="subtle">–</span>}
    </td>
  );
}

function TrafficRow({ row, selected, onSelect }: { row: Row; selected: boolean; onSelect: () => void }) {
  const record = row.record;
  const failed = record ? isFailure(record) : false;
  const elapsed = record ? record.total_ms : row.live ? Date.now() - row.live.started_at * 1000 : null;
  return (
    <tr className={selected ? "selected" : undefined} onClick={onSelect}>
      <td>
        <span className="title-line">
          <span className={`pill category category-${row.category}`}>{CATEGORY_LABELS[row.category]}</span>
          <button
            type="button"
            className="link"
            aria-pressed={selected}
            title={row.host}
            onClick={(event) => {
              event.stopPropagation();
              onSelect();
            }}
          >
            {row.service}
          </button>
          {row.model && <span className="pill accent">{row.model}</span>}
          {row.repeats?.length ? (
            <span className="pill" title={`${row.repeats.length + 1} identical requests in a row; showing the newest`}>
              ×{row.repeats.length + 1}
            </span>
          ) : null}
        </span>
        <small className="mono traffic-path" title={`${row.method} ${row.host}${row.path}`}>
          <span className="method">{row.method}</span> {row.path}
        </small>
        <small>
          <RouteBadge item={{ backend: row.record?.backend ?? row.live?.backend ?? "", client: row.client, kind: row.kind as RequestRecord["kind"] }} />{" "}
          {row.service !== row.host && <span className="mono">{row.host} · </span>}
          {row.client} #{row.request_id}
          {row.kind === "tunnel" ? ", not decrypted" : row.kind === "upgrade" ? ", WebSocket" : ""}
        </small>
      </td>
      <td className="nowrap">
        {record ? (
          <span className={`pill ${failed ? "bad" : "good"}`} title={outcomeLabel(record) ?? undefined}>
            {record.status}
          </span>
        ) : (
          <span className="pill live">pending</span>
        )}
        {/* Time and size sit under the status, leaving the columns to the token counts. */}
        <small>{formatDuration(elapsed)}</small>
        <small className="subtle">{new Date(row.started).toLocaleTimeString()}</small>
        {record && (
          <small className="subtle" title={`${formatBytes(record.request_bytes)} sent, ${formatBytes(record.response_bytes)} received`}>
            ↑ {formatBytes(record.request_bytes)} ↓ {formatBytes(record.response_bytes)}
          </small>
        )}
      </td>
      <TokenCell value={record?.input_tokens} estimated={record?.tokens_estimated} />
      <TokenCell value={record?.output_tokens} estimated={record?.tokens_estimated} />
      <TokenCell value={record?.cache_read_tokens} />
      <TokenCell value={record?.cache_creation_tokens} />
    </tr>
  );
}
