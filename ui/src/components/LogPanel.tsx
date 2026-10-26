import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { formatNumber } from "../format";
import { useClipboard } from "../hooks";

/** How a line is coloured: an error, a warning, routine noise, or plain. */
export type LogTone = "bad" | "warn" | "muted" | "";

export interface LogView {
  id: string;
  label: string;
  /** Lines this view keeps; every line when omitted. */
  keep?: (tone: LogTone) => boolean;
}

export const ALL_LINES: LogView = { id: "all", label: "All lines" };
export const PROBLEMS: LogView = { id: "problems", label: "Warnings and errors", keep: (tone) => tone === "bad" || tone === "warn" };
export const quietView = (label: string): LogView => ({ id: "quiet", label, keep: (tone) => tone !== "muted" });

/** A live server log: polled, filterable, and following the end like a terminal. */
export function LogPanel({
  queryKey,
  load,
  tone,
  views,
  defaultView,
  empty,
  footer,
  controls,
}: {
  queryKey: readonly unknown[];
  load: () => Promise<string>;
  tone: (line: string) => LogTone;
  views: LogView[];
  defaultView?: string;
  /** Shown instead of the log while it has no lines. */
  empty: ReactNode;
  /** Where the log comes from. */
  footer: ReactNode;
  /** Extra toolbar controls, such as which log to show. */
  controls?: ReactNode;
}) {
  const log = useQuery({ queryKey, queryFn: load, refetchInterval: 2000, gcTime: 0 });
  const copy = useClipboard();
  const pre = useRef<HTMLPreElement>(null);
  const [follow, setFollow] = useState(true);
  const [viewId, setViewId] = useState(defaultView ?? views[0].id);
  const [query, setQuery] = useState("");
  const view = views.find((item) => item.id === viewId) ?? views[0];

  const lines = useMemo(() => {
    const text = query.trim().toLowerCase();
    return (log.data ?? "")
      .split(/\r?\n/)
      .map((line) => ({ line, tone: tone(line) }))
      .filter((item) => item.line && (!view.keep || view.keep(item.tone)) && (!text || item.line.toLowerCase().includes(text)));
  }, [log.data, view, query, tone]);

  useEffect(() => {
    if (follow && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [lines, follow]);

  // Scrolling up pauses follow, scrolling back to the end resumes it.
  const onScroll = () => {
    const element = pre.current;
    if (element) setFollow(element.scrollHeight - element.scrollTop - element.clientHeight < 24);
  };

  const isEmpty = log.isSuccess && !log.data;
  return (
    <div className="tab-stack">
      <div className="filters log-toolbar">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter the log" aria-label="Filter the log" />
        {controls}
        <select value={view.id} onChange={(event) => setViewId(event.target.value)} aria-label="Lines to show">
          {views.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
        <label className="check">
          <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
          Follow
        </label>
        <button type="button" disabled={!lines.length} onClick={() => void copy(lines.map(({ line }) => line).join("\n"), "Log copied")}>
          Copy
        </button>
      </div>
      {log.isError ? (
        <p className="bad">{(log.error as Error).message}</p>
      ) : isEmpty ? (
        <p className="empty">{empty}</p>
      ) : (
        <pre ref={pre} className="server-log" onScroll={onScroll}>
          {log.isPending
            ? "Loading…"
            : lines.length
              ? lines.map((item, index) => (
                  <span key={index} className={item.tone ? `log-${item.tone}` : undefined}>
                    {item.line}
                    {"\n"}
                  </span>
                ))
              : "No lines match this view."}
        </pre>
      )}
      <small>
        {log.isSuccess && !isEmpty && `${formatNumber(lines.length)} lines shown. `}
        {footer}
      </small>
    </div>
  );
}
