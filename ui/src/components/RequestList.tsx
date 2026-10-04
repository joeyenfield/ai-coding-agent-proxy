import { useState } from "react";
import { Link } from "react-router-dom";
import { isFailure, outcomeLabel, tokens } from "../api";
import { formatBytes, formatDuration, formatRelative } from "../format";
import { useSessions } from "../hooks";
import { LiveCard } from "../pages/Live";
import { CATEGORY_LABELS, type Row } from "../traffic";
import { ExpandRow } from "./ExpandRow";
import { Inspector } from "./Inspector";

export const requestPath = (row: { session_id: string; request_id: string }) =>
  `/requests/${encodeURIComponent(row.session_id)}/${encodeURIComponent(row.request_id)}`;

/**
 * Requests as a single column of collapsed rows. Rows keep their open state by key,
 * so a request stays expanded as it moves from streaming to finished.
 */
export function RequestList({ rows, limit = 50 }: { rows: Row[]; limit?: number }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const [shown, setShown] = useState(limit);
  const sessions = useSessions();
  const toggle = (key: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  if (!rows.length) return null;
  return (
    <>
      <ul className="xlist" aria-label="Requests">
        {rows.slice(0, shown).map((row) => {
          const record = row.record;
          const streaming = !record;
          return (
            <ExpandRow
              key={row.key}
              expanded={open.has(row.key)}
              onToggle={() => toggle(row.key)}
              tone={streaming ? "live" : record && isFailure(record) ? "bad" : undefined}
              summary={<RequestSummary row={row} />}
              actions={
                <Link className="button" to={requestPath(row)}>
                  Open
                </Link>
              }
            >
              {row.live && !row.live.done ? (
                <LiveCard entry={row.live} />
              ) : record ? (
                <Inspector record={record} session={sessions.data?.find((session) => session.session_id === row.session_id)} />
              ) : null}
            </ExpandRow>
          );
        })}
      </ul>
      {rows.length > shown && (
        <p className="row">
          <button type="button" onClick={() => setShown((value) => value + limit)}>
            Show {Math.min(limit, rows.length - shown)} more
          </button>
          <span className="subtle">
            {shown} of {rows.length}
          </span>
        </p>
      )}
    </>
  );
}

function RequestSummary({ row }: { row: Row }) {
  const record = row.record;
  const elapsed = record ? record.total_ms : row.live ? Date.now() - row.live.started_at * 1000 : null;
  return (
    <>
      <span className="xrow-main">
        {record ? (
          <span className={`pill ${isFailure(record) ? "bad" : "good"}`} title={outcomeLabel(record) ?? undefined}>
            {record.status}
          </span>
        ) : (
          <span className="pill live">
            <span className="live-mark" aria-hidden="true" /> streaming
          </span>
        )}
        <span className={`pill category category-${row.category}`}>{CATEGORY_LABELS[row.category]}</span>
        <strong>{row.model || row.service}</strong>
        <span className="mono subtle xrow-path" title={`${row.method} ${row.host}${row.path}`}>
          {row.method} {row.path}
        </span>
        {row.repeats?.length ? <span className="pill">×{row.repeats.length + 1}</span> : null}
      </span>
      <span className="xrow-meta">
        {record && (record.input_tokens > 0 || record.output_tokens > 0) && (
          <span>
            {tokens(record, record.input_tokens)} in, {tokens(record, record.output_tokens)} out
          </span>
        )}
        {record && <span>{formatBytes(record.response_bytes)}</span>}
        <span>{formatDuration(elapsed)}</span>
        <span className="subtle">#{row.request_id}</span>
        <span className="subtle">{formatRelative(new Date(row.started).toISOString())}</span>
      </span>
    </>
  );
}
