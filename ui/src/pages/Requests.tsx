import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { isFailure, outcomeLabel, requestKey, tokens, type RequestRecord } from "../api";
import { Inspector } from "../components/Inspector";
import { endpointPath, formatDuration, formatNumber, formatRelative, shortId } from "../format";
import { useRequests, useSessions } from "../hooks";

const PAGE_SIZE = 25;

export function RequestsPage() {
  const requests = useRequests();
  const sessions = useSessions();
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(0);
  const sessionFilter = params.get("session") ?? "";
  const kind = params.get("show") ?? "";
  const selectedKey = params.get("request");

  const records = requests.data ?? [];
  const matches = useMemo(() => {
    const text = query.trim().toLowerCase();
    return records.filter(
      (record) =>
        (!sessionFilter || record.session_id === sessionFilter) &&
        (!text ||
          [record.request_id, record.session_id, record.client, record.model, record.backend, record.endpoint].some((value) =>
            String(value ?? "").toLowerCase().includes(text),
          )) &&
        (!kind || (kind === "traced" ? record.trace_available : kind === "untraced" ? !record.trace_available : isFailure(record))),
    );
  }, [records, query, sessionFilter, kind]);
  const pages = Math.max(1, Math.ceil(matches.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const visible = matches.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
  const selected = records.find((record) => requestKey(record) === selectedKey);

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: key !== "request" });
    if (key !== "request") setPage(0);
  };

  return (
    <div className="page wide">
      <header className="page-head">
        <h1>Request history</h1>
        <p className="lede">
          {formatNumber(records.length)} saved requests across {formatNumber(sessions.data?.length ?? 0)} sessions.
        </p>
      </header>
      <div className="split">
        <div className="split-main">
          <div className="filters">
            <input
              type="search"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setPage(0);
              }}
              placeholder="Search model, agent, endpoint or ID"
              aria-label="Search requests"
            />
            <select value={sessionFilter} onChange={(event) => setParam("session", event.target.value)} aria-label="Session">
              <option value="">All sessions</option>
              {sessions.data?.map((session) => (
                <option key={session.session_id} value={session.session_id}>
                  {session.client} {shortId(session.session_id)}
                </option>
              ))}
            </select>
            <select value={kind} onChange={(event) => setParam("show", event.target.value)} aria-label="Show">
              <option value="">All requests</option>
              <option value="traced">With payload</option>
              <option value="untraced">Without payload</option>
              <option value="errors">Failed</option>
            </select>
          </div>
          {requests.isError ? (
            <p className="empty bad">{requests.error.message}</p>
          ) : !visible.length ? (
            <p className="empty">{records.length ? "No requests match these filters." : "No requests have been recorded yet."}</p>
          ) : (
            <table className="table selectable">
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col">Agent</th>
                  <th scope="col" className="num">Duration</th>
                  <th scope="col" className="num">Output</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((record) => (
                  <Row
                    key={requestKey(record)}
                    record={record}
                    selected={requestKey(record) === selectedKey}
                    onSelect={() => setParam("request", requestKey(record))}
                  />
                ))}
              </tbody>
            </table>
          )}
          <div className="pager">
            <span className="subtle">
              {matches.length
                ? `${current * PAGE_SIZE + 1}–${Math.min((current + 1) * PAGE_SIZE, matches.length)} of ${formatNumber(matches.length)}`
                : "0 requests"}
            </span>
            <div className="row">
              <button type="button" disabled={current === 0} onClick={() => setPage(current - 1)}>
                Previous
              </button>
              <button type="button" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
                Next
              </button>
            </div>
          </div>
        </div>
        <div className="split-side">
          {selected ? (
            <Inspector
              key={requestKey(selected)}
              record={selected}
              session={sessions.data?.find((session) => session.session_id === selected.session_id)}
            />
          ) : (
            <div className="inspector placeholder">Select a request to see its timing, tokens and payload.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function Row({ record, selected, onSelect }: { record: RequestRecord; selected: boolean; onSelect: () => void }) {
  const failed = isFailure(record);
  return (
    <tr className={selected ? "selected" : undefined} onClick={onSelect}>
      <td>
        <span className="title-line">
          <button
            type="button"
            className="link"
            aria-pressed={selected}
            onClick={(event) => {
              event.stopPropagation();
              onSelect();
            }}
          >
            {record.model || "Unknown model"}
          </button>
          {failed ? <span className="pill bad">{outcomeLabel(record)}</span> : record.trace_available && <span className="pill">Payload</span>}
        </span>
        <small className="mono">
          #{record.request_id} {endpointPath(record.endpoint)}
        </small>
      </td>
      <td>
        {record.client}
        <small>{record.backend}</small>
      </td>
      <td className="num nowrap">
        {formatDuration(record.total_ms)}
        <small>{formatRelative(record.timestamp)}</small>
      </td>
      <td className="num nowrap">
        {tokens(record, record.output_tokens)} tok
        <small>{record.generation_tps == null ? "–" : `${formatNumber(record.generation_tps)}/s`}</small>
      </td>
    </tr>
  );
}
