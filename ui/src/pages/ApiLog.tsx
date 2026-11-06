import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type LoggedBody, type RequestLogEntry, type RequestLogFilters, type RequestLogPayloads } from "../api";
import { PartTag } from "../components/PartTag";
import { formatDuration, formatNumber } from "../format";
import { answer, OUTCOME_CLASS, OUTCOME_LABEL, span, when } from "./AccessLog";
import { useOlderPages } from "../hooks";

const apiLogId = (entry: RequestLogEntry) => entry.id;

const PAGE = 100;
const POLL_MS = 5000;
const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "WS"];
const STATUSES: { value: string; label: string }[] = [
  { value: "", label: "Any answer" },
  { value: "2xx", label: "2xx success" },
  { value: "3xx", label: "3xx redirect" },
  { value: "4xx", label: "4xx refused" },
  { value: "5xx", label: "5xx server error" },
  { value: "failed", label: "Failed or dropped" },
  { value: "websocket", label: "WebSockets" },
];

const forbiddenError = (error: unknown) => error instanceof ApiError && error.status === 403;

/** Server › API log: every request the server saved, for as many days as its setting says (site admins). */
export function ApiLogPage() {
  const summary = useQuery({
    queryKey: ["request-log-summary"],
    queryFn: api.requestLogSummary,
    refetchInterval: (query) => (forbiddenError(query.state.error) ? false : 30000),
    retry: false,
  });
  const forbidden = forbiddenError(summary.error);
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>API log</h1>
        <p className="lede">
          Every request the server received and saved, from anyone: the console, agents, machines and their tunnels, sign-ins, and the
          ones refused. Kept for as many days as you choose. In debug mode the headers and bodies are kept too, for a shorter time.
        </p>
      </header>
      {forbidden ? (
        <p className="empty">Only a site admin can see the API log.</p>
      ) : (
        <>
          {summary.isError && <p className="bad">Couldn't read the API log: {summary.error.message}</p>}
          <LogSettings />
          <Requests />
        </>
      )}
    </div>
  );
}

/** What's being saved and for how long, with debug mode one click away. */
function LogSettings() {
  const client = useQueryClient();
  const summary = useQuery({ queryKey: ["request-log-summary"], queryFn: api.requestLogSummary, retry: false });
  const toggle = useMutation({
    mutationFn: (changes: { enabled?: boolean; payloads?: boolean }) => api.saveRequestLog(changes),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["request-log-summary"] });
      void client.invalidateQueries({ queryKey: ["storage"] });
    },
  });
  const data = summary.data;
  if (!data) return null;
  const { settings } = data;
  return (
    <section className="panel" aria-label="What's saved">
      <dl className="fact-list">
        <div>
          <dt>Saving</dt>
          <dd className={settings.enabled ? undefined : "warn"}>
            {settings.enabled ? `Every request, kept ${settings.days} ${settings.days === 1 ? "day" : "days"}` : "Off: nothing is saved until you start it"}
          </dd>
        </div>
        <div>
          <dt>Saved now</dt>
          <dd>
            {formatNumber(data.count)} {data.count === 1 ? "request" : "requests"}
            {data.oldest ? `, since ${when(data.oldest)}` : ""}
          </dd>
        </div>
        <div>
          <dt>Debug mode (payloads)</dt>
          <dd className={settings.payloads ? "warn" : undefined}>
            {settings.payloads ? `On: headers and bodies kept ${settings.payload_days} ${settings.payload_days === 1 ? "day" : "days"}` : "Off"}
            {data.payloads > 0 && ` · ${formatNumber(data.payloads)} saved`}
          </dd>
        </div>
      </dl>
      <div className="row wrap">
        <button type="button" className={settings.enabled ? undefined : "primary"} disabled={toggle.isPending} onClick={() => toggle.mutate({ enabled: !settings.enabled })}>
          {settings.enabled ? "Stop saving requests" : "Start saving requests"}
        </button>
        {settings.enabled && (
          <button type="button" disabled={toggle.isPending} onClick={() => toggle.mutate({ payloads: !settings.payloads })}>
            {settings.payloads ? "Turn debug mode off" : "Turn debug mode on"}
          </button>
        )}
        <Link to="/settings?tab=storage">Change how long they're kept</Link>
        {toggle.isError && <span className="bad">{toggle.error.message}</span>}
      </div>
      {settings.payloads && (
        <p className="subtle">
          Bodies are saved as they are, up to 64 KB each, except sign-in, credential headers and fields named like secrets (passwords, tokens,
          keys). Turn it off when you've found what you were looking for.
        </p>
      )}
    </section>
  );
}

function useFilters() {
  const [search, setSearch] = useState("");
  const [user, setUser] = useState("");
  const [filters, setFilters] = useState<RequestLogFilters>({});
  const [method, setMethod] = useState("");
  const [status, setStatus] = useState("");
  // Typed words wait for a pause, so there isn't a request per key.
  useEffect(() => {
    const timer = setTimeout(() => setFilters({ q: search.trim(), user: user.trim(), method, status }), 300);
    return () => clearTimeout(timer);
  }, [search, user, method, status]);
  return { search, setSearch, user, setUser, method, setMethod, status, setStatus, filters };
}

function Requests() {
  const { filters, ...inputs } = useFilters();
  const [follow, setFollow] = useState(true);
  const key = JSON.stringify(filters);
  const log = useQuery({
    queryKey: ["request-log", key],
    queryFn: () => api.requestLog({ ...filters, limit: PAGE }),
    refetchInterval: (query) => (follow && !forbiddenError(query.state.error) ? POLL_MS : false),
    retry: false,
  });
  const newest = log.data?.entries ?? [];
  const { entries, loadOlder, olderError, exhausted } = useOlderPages(key, newest, apiLogId,
    async (last) => (await api.requestLog({ ...filters, limit: PAGE, before: last.time })).entries, PAGE);
  const filtered = Boolean(filters.q || filters.user || filters.method || filters.status);
  return (
    <section className="section" aria-labelledby="api-log-requests">
      <div className="section-head">
        <h2 id="api-log-requests">Requests</h2>
      </div>
      <div className="filters">
        <input
          type="search"
          value={inputs.search}
          onChange={(event) => inputs.setSearch(event.target.value)}
          placeholder="Search path, address, user agent, status or reason"
          aria-label="Search the API log"
        />
        <select value={inputs.method} onChange={(event) => inputs.setMethod(event.target.value)} aria-label="Method">
          <option value="">Any method</option>
          {METHODS.map((method) => (
            <option key={method} value={method}>
              {method === "WS" ? "WebSocket" : method}
            </option>
          ))}
        </select>
        <select value={inputs.status} onChange={(event) => inputs.setStatus(event.target.value)} aria-label="Answer">
          {STATUSES.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
        <input value={inputs.user} onChange={(event) => inputs.setUser(event.target.value)} placeholder="User" aria-label="User" autoCapitalize="none" />
        <label className="check">
          <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
          Follow
        </label>
      </div>
      {log.isError && <p className="bad">Couldn't read the API log: {log.error.message}</p>}
      {log.isPending ? (
        <p className="empty">Asking the server…</p>
      ) : !entries.length ? (
        <p className="empty">{filtered ? "Nothing matches." : "Nothing saved yet."}</p>
      ) : (
        <Entries entries={entries} />
      )}
      {entries.length >= PAGE && !exhausted && (
        <p>
          <button type="button" onClick={() => void loadOlder()}>
            Load older
          </button>
          {olderError && <span className="bad"> Couldn't load older entries: {olderError}</span>}
        </p>
      )}
    </section>
  );
}

const duration = (entry: RequestLogEntry) =>
  entry.duration_ms === null ? "–" : entry.kind === "websocket" ? span(entry.duration_ms) : formatDuration(entry.duration_ms);

function Entries({ entries }: { entries: RequestLogEntry[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="table-scroll">
      <table className="table selectable access-log">
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Request</th>
            <th scope="col">Answer</th>
            <th scope="col">From</th>
            <th scope="col" className="num">Duration</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <Fragment key={entry.id}>
              <tr className={open === entry.id ? "selected" : undefined} onClick={() => setOpen(open === entry.id ? null : entry.id)}>
                <td className="mono">{when(entry.time)}</td>
                <td className="mono wrap">
                  {entry.method} {entry.path}
                  {entry.query && <span className="subtle">?{entry.query}</span>}
                  {entry.payload && (
                    <>
                      {" "}
                      <span className="pill" title="Debug mode saved its headers and bodies">payload</span>
                    </>
                  )}
                </td>
                <td>
                  <span className={OUTCOME_CLASS[entry.outcome]}>{OUTCOME_LABEL[entry.outcome]}</span> <span className="mono">{answer(entry)}</span>
                </td>
                <td className="mono wrap">
                  {entry.machine_name && <div>{entry.machine_name}</div>}
                  {entry.user ?? <span className="subtle">not signed in</span>} · {entry.forwarded_for || entry.client}
                </td>
                <td className="num">{duration(entry)}</td>
              </tr>
              {open === entry.id && (
                <tr className="access-detail">
                  <td colSpan={5}>
                    <Detail entry={entry} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Detail({ entry }: { entry: RequestLogEntry }) {
  const full = useQuery({ queryKey: ["request-log-entry", entry.id], queryFn: () => api.requestLogEntry(entry.id), enabled: entry.payload });
  return (
    <>
      <dl className="access-facts">
        {(entry.close_reason || entry.detail) && (
          <div>
            <dt>Reason</dt>
            <dd className="mono wrap">{entry.close_reason || entry.detail}</dd>
          </div>
        )}
        <div>
          <dt>When</dt>
          <dd>{new Date(entry.time * 1000).toLocaleString()}</dd>
        </div>
        <div>
          <dt>Address</dt>
          <dd className="mono wrap">
            {entry.client}
            {entry.forwarded_for && ` (forwarded for ${entry.forwarded_for})`} to {entry.host || "–"}
          </dd>
        </div>
        <div>
          <dt>User</dt>
          <dd>{entry.user ? `${entry.user} (${entry.tenant})` : "Not signed in"}</dd>
        </div>
        <div>
          <dt>Program</dt>
          <dd className="mono wrap">
            {entry.user_agent || "–"}
            {entry.program_version && ` · version ${entry.program_version}, protocol ${entry.protocol || "?"}`}
          </dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{duration(entry)}</dd>
        </div>
      </dl>
      {!entry.payload ? (
        <p className="subtle">No payload: debug mode was off{entry.path.startsWith("/api/auth/") ? ", and sign-in is never saved" : ""}.</p>
      ) : full.isPending ? (
        <p className="empty">Reading the payload…</p>
      ) : full.isError ? (
        <p className="bad">{full.error.message}</p>
      ) : full.data?.payloads ? (
        <Payloads payloads={full.data.payloads} />
      ) : (
        <p className="subtle">Its payload was saved, but has since been deleted (debug mode keeps payloads for fewer days).</p>
      )}
    </>
  );
}

function Payloads({ payloads }: { payloads: RequestLogPayloads }) {
  return (
    <div className="api-log-payloads">
      <div>
        <h3>Request</h3>
        {payloads.request.query && <p className="mono wrap">?{payloads.request.query}</p>}
        <Headers headers={payloads.request.headers} />
        <Body body={payloads.request.body} />
      </div>
      <div>
        <h3>Response</h3>
        <Headers headers={payloads.response.headers} />
        <Body body={payloads.response.body} />
      </div>
    </div>
  );
}

function Headers({ headers }: { headers: Record<string, string> }) {
  const names = Object.keys(headers);
  if (!names.length) return null;
  return (
    <details>
      <summary>Headers ({names.length})</summary>
      <dl className="api-log-headers">
        {names.map((name) => (
          <div key={name}>
            <dt className="mono">{name}</dt>
            <dd className="mono wrap">{headers[name]}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function Body({ body }: { body: LoggedBody }) {
  if (!body.bytes) return <p className="subtle">No body.</p>;
  const size = `${formatNumber(body.bytes)} bytes${body.truncated ? ", the first 64 KB kept" : ""}`;
  if (body.omitted) return <p className="subtle">{body.omitted}, {size}: not kept.</p>;
  const text = body.json !== undefined ? JSON.stringify(body.json, null, 2) : body.text ?? "";
  return (
    <>
      <p className="subtle">Body, {size}</p>
      <pre className="code api-log-body">{text}</pre>
    </>
  );
}
