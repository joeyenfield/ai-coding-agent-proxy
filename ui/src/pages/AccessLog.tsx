import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, type AccessEntry, type AccessLogStats } from "../api";
import { PartTag } from "../components/PartTag";
import { TabBar, TabPanel, useTabParam } from "../components/Tabs";
import { formatDuration, formatNumber } from "../format";

const POLL_MS = 3000;
const PAGE = 200;
const TABS = ["overview", "run", "saved"] as const;
const FLEET_PATH = "/api/machines/connect";

const forbiddenError = (error: unknown) => error instanceof ApiError && error.status === 403;

/** What a request got back: its status, and for a WebSocket how it was closed and by whom. */
export function answer(entry: Omit<AccessEntry, "id" | "saved">): string {
  if (entry.kind === "http") return entry.status === null ? "" : String(entry.status);
  if (entry.outcome === "open") return entry.status === 101 ? "Open" : "Connecting";
  if (entry.status !== null && entry.status !== 101) return `Refused (${entry.status}${entry.close_code !== null ? `, ${entry.close_code}` : ""})`;
  if (entry.close_code === null) return "Closed";
  return `${entry.close_code} by ${entry.closed_by ?? "?"}`;
}

export const OUTCOME_CLASS: Record<AccessEntry["outcome"], string> = { ok: "pill good", failed: "pill bad", dropped: "pill live", open: "pill" };
export const OUTCOME_LABEL: Record<AccessEntry["outcome"], string> = { ok: "OK", failed: "Failed", dropped: "Dropped", open: "Open" };

const clock = (seconds: number) => new Date(seconds * 1000).toLocaleTimeString();
export const when = (seconds: number | null) => {
  if (!seconds) return "–";
  const date = new Date(seconds * 1000);
  return date.toDateString() === new Date().toDateString() ? date.toLocaleTimeString() : date.toLocaleString();
};

/** Days, hours and minutes (or seconds) for a span in milliseconds. */
export function span(ms: number) {
  const minutes = Math.floor(ms / 60000);
  if (minutes < 1) return `${Math.round(ms / 1000)} s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 1) return `${minutes} min`;
  const days = Math.floor(hours / 24);
  return days ? `${days} d ${hours % 24} h` : `${hours} h ${minutes % 60} min`;
}

/** Server › Access log: every request the server received, the refused ones too, for site admins. */
export function AccessLogPage() {
  const [tab, setTab] = useTabParam(TABS);
  const stats = useQuery({
    queryKey: ["access-log-stats"],
    queryFn: api.accessLogStats,
    refetchInterval: (query) => (forbiddenError(query.state.error) ? false : 10000),
    retry: false,
  });
  const forbidden = forbiddenError(stats.error);
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Access log</h1>
        <p className="lede">
          Every HTTP request and WebSocket the server received, and how each ended: the console's calls, sign-ins, machines' fleet
          connections and tunnels, and the ones refused before they got anywhere. Everything since the server started is kept in memory;
          failures, dropped connections and every fleet connection are also saved, so they outlast a restart.
        </p>
      </header>
      {forbidden ? (
        <p className="empty">Only a site admin can see the access log.</p>
      ) : (
        <>
          <TabBar
            label="Access log"
            idPrefix="access-"
            active={tab}
            onSelect={setTab}
            tabs={[
              { id: "overview", label: "Overview" },
              { id: "run", label: "Since this start" },
              { id: "saved", label: "Saved history" },
            ]}
          />
          <TabPanel idPrefix="access-" id="overview" active={tab === "overview"} className="access-panel">
            {stats.isPending && <p className="empty">Asking the server…</p>}
            {stats.isError && <p className="bad">Couldn't read the access log: {stats.error.message}</p>}
            {stats.data && <Overview stats={stats.data} />}
          </TabPanel>
          <TabPanel idPrefix="access-" id="run" active={tab === "run"} className="access-panel">
            <ThisRun />
          </TabPanel>
          <TabPanel idPrefix="access-" id="saved" active={tab === "saved"} className="access-panel">
            <Saved keptDays={stats.data?.saved.kept_days} />
          </TabPanel>
        </>
      )}
    </div>
  );
}

function Overview({ stats }: { stats: AccessLogStats }) {
  const { totals, durations, saved } = stats;
  const openFleet = stats.open_websockets.filter((socket) => socket.path === FLEET_PATH);
  // The same machine: by its id when its hello named it, else by its sign-in and address.
  const same = (a: FleetKey, b: FleetKey) => (a.machine_id || b.machine_id ? a.machine_id === b.machine_id : a.user === b.user && a.address === b.address);
  const connectedNow = (item: FleetKey) => openFleet.some((socket) => same(socket, item));
  const lastHour = stats.per_minute.reduce(
    (sum, minute) => ({ all: sum.all + minute.ok + minute.failed + minute.dropped, bad: sum.bad + minute.failed + minute.dropped }),
    { all: 0, bad: 0 },
  );
  // Machines connected now that have no saved connection yet (one that hasn't ended since the first start).
  const openOnly = openFleet.filter((socket) => !saved.fleet.some((item) => same(item, socket)));
  return (
    <>
      <section className="section">
        <dl className="fact-list">
          <div>
            <dt>Since</dt>
            <dd>{new Date(stats.started_at * 1000).toLocaleString()}</dd>
          </div>
          <div>
            <dt>OK</dt>
            <dd className="good">{formatNumber(totals.ok)}</dd>
          </div>
          <div>
            <dt>Failed</dt>
            <dd className={totals.failed ? "bad" : undefined}>{formatNumber(totals.failed)}</dd>
          </div>
          <div>
            <dt>WebSockets dropped</dt>
            <dd className={totals.dropped ? "warn" : undefined}>{formatNumber(totals.dropped)}</dd>
          </div>
          <div>
            <dt>WebSockets open now</dt>
            <dd>
              {formatNumber(totals.open_websockets)} ({formatNumber(openFleet.length)} {openFleet.length === 1 ? "machine" : "machines"})
            </dd>
          </div>
          <div>
            <dt>Answer time, median / 95%</dt>
            <dd>{durations.p50 === null ? "–" : `${formatDuration(durations.p50)} / ${formatDuration(durations.p95)}`}</dd>
          </div>
        </dl>
      </section>

      <section className="section">
        <h2>The last hour</h2>
        <p className="subtle">
          {formatNumber(lastHour.all)} requests, {formatNumber(lastHour.bad)} failed or dropped. Each bar is a minute.
        </p>
        <Bars
          items={stats.per_minute.map((minute) => ({ label: clock(minute.minute), ok: minute.ok, failed: minute.failed, dropped: minute.dropped }))}
          caption="Requests a minute in the last hour"
        />
      </section>

      <section className="section">
        <h2>Machines' fleet connections</h2>
        <p className="subtle">
          Each agent-machine's connections to the server over the last {saved.fleet_days} days, saved across restarts. Connections
          refused before the machine said who it is are listed by their sign-in and address. If a machine shown offline has no row here,
          it isn't reaching this server at all.
        </p>
        {!saved.fleet.length && !openFleet.length ? (
          <p className="empty">No fleet connections yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="table access-fleet">
              <thead>
                <tr>
                  <th scope="col">Machine</th>
                  <th scope="col">Now</th>
                  <th scope="col" className="num">Connections</th>
                  <th scope="col" className="num">Failed</th>
                  <th scope="col" className="num">Dropped</th>
                  <th scope="col">Last ended</th>
                  <th scope="col" className="num">Time connected</th>
                </tr>
              </thead>
              <tbody>
                {openOnly.map((socket) => (
                  <tr key={`open-${socket.machine_id ?? ""}-${socket.user}-${socket.address}-${socket.since}`}>
                    <td className="wrap">
                      <FleetName item={socket} />
                    </td>
                    <td>
                      <span className="pill good">Connected</span>
                    </td>
                    <td className="num">–</td>
                    <td className="num">–</td>
                    <td className="num">–</td>
                    <td>Open since {when(socket.since)}</td>
                    <td className="num">{span(Date.now() - socket.since * 1000)}</td>
                  </tr>
                ))}
                {saved.fleet.map((item) => (
                  <tr key={`${item.machine_id ?? ""}-${item.user}-${item.address}`}>
                    <td className="wrap">
                      <FleetName item={item} />
                    </td>
                    <td>
                      {connectedNow(item) ? <span className="pill good">Connected</span> : <span className="pill">Not connected</span>}
                    </td>
                    <td className="num">{formatNumber(item.connections)}</td>
                    <td className={`num ${item.failed ? "bad" : ""}`}>{formatNumber(item.failed)}</td>
                    <td className={`num ${item.dropped ? "warn" : ""}`}>{formatNumber(item.dropped)}</td>
                    <td>
                      <span className="mono">{item.last_answer}</span> {when(item.last_time)}
                      {item.last_reason && <div className="subtle wrap">{item.last_reason}</div>}
                    </td>
                    <td className="num">{span(item.connected_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="access-columns">
        <section className="section">
          <h2>What fails most</h2>
          {!stats.failing_routes.length ? (
            <p className="empty">Nothing has failed since the server started.</p>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Request</th>
                    <th scope="col">Answer</th>
                    <th scope="col" className="num">Times</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.failing_routes.map((item) => (
                    <tr key={`${item.method} ${item.route} ${item.answer}`}>
                      <td className="mono wrap">
                        {item.method} {item.route}
                        {item.reason && <div className="subtle">{item.reason}</div>}
                      </td>
                      <td className="mono">{item.answer}</td>
                      <td className="num">{formatNumber(item.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <section className="section">
          <h2>Where failures come from</h2>
          {!stats.failing_clients.length ? (
            <p className="empty">Nothing has failed since the server started.</p>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Address</th>
                    <th scope="col">Last</th>
                    <th scope="col" className="num">Times</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.failing_clients.map((item) => (
                    <tr key={item.address}>
                      <td className="mono wrap">
                        {item.address}
                        <div className="subtle">{item.user ?? "not signed in"}</div>
                      </td>
                      <td className="wrap">
                        {when(item.time)}
                        {item.user_agent && <div className="subtle mono">{item.user_agent.slice(0, 60)}</div>}
                      </td>
                      <td className="num">{formatNumber(item.count)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      <section className="section">
        <h2>Answers</h2>
        <div className="chips">
          {stats.answers.map((item) => (
            <span key={item.answer} className={`pill mono ${badAnswer(item.answer) ? "bad" : ""}`}>
              {item.answer}: {formatNumber(item.count)}
            </span>
          ))}
        </div>
      </section>

      <section className="section">
        <h2>Saved, by day</h2>
        <p className="subtle">
          {formatNumber(saved.count)} entries saved{saved.oldest ? `, the oldest from ${new Date(saved.oldest * 1000).toLocaleString()}` : ""}.
          Kept for {saved.kept_days} days (the storage setting, or 30 days when recordings are kept forever). Only failures, dropped
          WebSockets and fleet connections are saved, so OK here means fleet connections that ended normally.
        </p>
        <Bars items={saved.days.map((day) => ({ label: day.day, ok: day.ok, failed: day.failed, dropped: day.dropped }))} caption="Saved entries a day" />
      </section>
    </>
  );
}

interface FleetKey {
  machine_id: string | null;
  machine_name: string | null;
  user: string | null;
  address: string;
  program_version: string;
}

/** A machine by its name (with a link to its page), else "Before its hello"; then its sign-in, address and version. */
function FleetName({ item }: { item: FleetKey }) {
  return (
    <>
      {item.machine_id ? <Link to={`/machines/${encodeURIComponent(item.machine_id)}`}>{item.machine_name || item.machine_id}</Link> : <span className="subtle">Before its hello</span>}
      <div className="subtle mono">
        {item.user ?? "not signed in"} · {item.address}
        {item.program_version && ` · ${item.program_version}`}
      </div>
    </>
  );
}

/** An error status, a refusal, or a WebSocket the server closed with a code other than a normal one. */
const badAnswer = (label: string) => /^[45]\d\d$/.test(label) || label.startsWith("refused") || (/ by server$/.test(label) && !/^100[01] /.test(label));

/** Stacked bars: OK, failed and dropped, each bar labelled on hover. */
function Bars({ items, caption }: { items: { label: string; ok: number; failed: number; dropped: number }[]; caption: string }) {
  const top = Math.max(1, ...items.map((item) => item.ok + item.failed + item.dropped));
  return (
    <figure className="access-bars" aria-label={caption}>
      <div className="access-bars-plot">
        {items.map((item) => (
          <div key={item.label} className="access-bar" title={`${item.label}: ${item.ok} OK, ${item.failed} failed, ${item.dropped} dropped`}>
            <span className="access-bar-ok" style={{ height: `${(item.ok / top) * 100}%` }} />
            <span className="access-bar-dropped" style={{ height: `${(item.dropped / top) * 100}%` }} />
            <span className="access-bar-failed" style={{ height: `${(item.failed / top) * 100}%` }} />
          </div>
        ))}
      </div>
      <figcaption className="access-bars-caption subtle">
        <span>{items[0]?.label}</span>
        <span className="access-legend">
          <i className="access-bar-ok" /> OK <i className="access-bar-failed" /> Failed <i className="access-bar-dropped" /> Dropped · top {formatNumber(top)}
        </span>
        <span>{items[items.length - 1]?.label}</span>
      </figcaption>
    </figure>
  );
}

/** A search box and the failed-only choice, searching as you type without a request per key. */
function useFilters() {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [failedOnly, setFailedOnly] = useState(false);
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);
  return { search, setSearch, query, failedOnly, setFailedOnly };
}

function ThisRun() {
  const filters = useFilters();
  const [follow, setFollow] = useState(true);
  const [older, setOlder] = useState<AccessEntry[]>([]);
  const [exhausted, setExhausted] = useState(false);
  useEffect(() => {
    setOlder([]);
    setExhausted(false);
  }, [filters.failedOnly, filters.query]);
  const log = useQuery({
    queryKey: ["access-log", filters.failedOnly, filters.query],
    queryFn: () => api.accessLog({ failed: filters.failedOnly, q: filters.query, limit: PAGE }),
    refetchInterval: (query) => (follow && !forbiddenError(query.state.error) ? POLL_MS : false),
    retry: false,
  });
  const newest = log.data?.entries ?? [];
  const seen = new Set(newest.map((entry) => entry.id));
  const entries = [...newest, ...older.filter((entry) => !seen.has(entry.id))];
  const loadOlder = async () => {
    const last = entries[entries.length - 1];
    if (!last || typeof last.id !== "number") return;
    const page = await api.accessLog({ failed: filters.failedOnly, q: filters.query, limit: PAGE, before: last.id });
    setOlder((current) => [...current, ...page.entries]);
    if (page.entries.length < PAGE) setExhausted(true);
  };
  return (
    <>
      {log.data && (
        <p className="subtle">
          Everything since {new Date(log.data.started_at * 1000).toLocaleString()}, in memory: the newest {formatNumber(log.data.kept.all)}{" "}
          requests, and the newest {formatNumber(log.data.kept.failed)} failures apart from them.
        </p>
      )}
      <Filters filters={filters} failedLabel="Failed and dropped">
        <label className="check">
          <input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />
          Follow
        </label>
      </Filters>
      {log.isError && <p className="bad">Couldn't read the access log: {log.error.message}</p>}
      <Entries entries={entries} pending={log.isPending} filtered={Boolean(filters.query || filters.failedOnly)} />
      {entries.length >= PAGE && !exhausted && (
        <p>
          <button type="button" onClick={() => void loadOlder()}>
            Load older
          </button>
        </p>
      )}
    </>
  );
}

function Saved({ keptDays }: { keptDays?: number }) {
  const filters = useFilters();
  const [fleetOnly, setFleetOnly] = useState(false);
  const [older, setOlder] = useState<AccessEntry[]>([]);
  const [exhausted, setExhausted] = useState(false);
  useEffect(() => {
    setOlder([]);
    setExhausted(false);
  }, [filters.failedOnly, filters.query, fleetOnly]);
  const options = { failed: filters.failedOnly, fleet: fleetOnly, q: filters.query, limit: PAGE };
  const log = useQuery({
    queryKey: ["access-log-history", filters.failedOnly, fleetOnly, filters.query],
    queryFn: () => api.accessLogHistory(options),
    refetchInterval: 15000,
    retry: false,
  });
  const newest = log.data?.entries ?? [];
  const seen = new Set(newest.map((entry) => entry.id));
  const entries = [...newest, ...older.filter((entry) => !seen.has(entry.id))];
  const loadOlder = async () => {
    const last = entries[entries.length - 1];
    if (!last) return;
    const page = await api.accessLogHistory({ ...options, before: last.time });
    setOlder((current) => [...current, ...page.entries]);
    if (page.entries.length < PAGE) setExhausted(true);
  };
  return (
    <>
      <p className="subtle">
        Saved in the database, across restarts: failures, dropped WebSockets and every machine's fleet connection, however it ended.
        {keptDays ? ` Kept for ${keptDays} days.` : ""} The console's own successful calls aren't saved.
      </p>
      <Filters filters={filters} failedLabel="Failed and dropped only">
        <label className="check">
          <input type="checkbox" checked={fleetOnly} onChange={(event) => setFleetOnly(event.target.checked)} />
          Fleet connections only
        </label>
      </Filters>
      {log.isError && <p className="bad">Couldn't read the saved history: {log.error.message}</p>}
      <Entries entries={entries} pending={log.isPending} filtered={Boolean(filters.query || filters.failedOnly || fleetOnly)} withDate />
      {entries.length >= PAGE && !exhausted && (
        <p>
          <button type="button" onClick={() => void loadOlder()}>
            Load older
          </button>
        </p>
      )}
    </>
  );
}

function Filters({ filters, failedLabel, children }: { filters: ReturnType<typeof useFilters>; failedLabel: string; children?: ReactNode }) {
  return (
    <div className="filters">
      <input
        type="search"
        value={filters.search}
        onChange={(event) => filters.setSearch(event.target.value)}
        placeholder="Search path, address, user, user agent, status or reason"
        aria-label="Search the access log"
      />
      <select value={filters.failedOnly ? "failed" : ""} onChange={(event) => filters.setFailedOnly(event.target.value === "failed")} aria-label="Show">
        <option value="">All</option>
        <option value="failed">{failedLabel}</option>
      </select>
      {children}
    </div>
  );
}

function Entries({ entries, pending, filtered, withDate = false }: { entries: AccessEntry[]; pending: boolean; filtered: boolean; withDate?: boolean }) {
  const [open, setOpen] = useState<AccessEntry["id"] | null>(null);
  if (pending) return <p className="empty">Asking the server…</p>;
  if (!entries.length) return <p className="empty">{filtered ? "Nothing matches." : "Nothing yet."}</p>;
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
                <td className="mono">{withDate ? when(entry.time) : clock(entry.time)}</td>
                <td className="mono wrap">
                  {entry.method} {entry.path}
                  {entry.query && <span className="subtle">?{entry.query}</span>}
                </td>
                <td>
                  <span className={OUTCOME_CLASS[entry.outcome]}>
                    {entry.outcome === "open" && entry.kind === "http" ? "Answering" : OUTCOME_LABEL[entry.outcome]}
                  </span>{" "}
                  <span className="mono">{answer(entry)}</span>
                </td>
                <td className="mono wrap">
                  {entry.machine_name && <div>{entry.machine_name}</div>}
                  {entry.user ?? <span className="subtle">not signed in</span>} · {entry.forwarded_for || entry.client}
                </td>
                <td className="num">{entry.duration_ms === null ? "–" : entry.kind === "websocket" ? span(entry.duration_ms) : formatDuration(entry.duration_ms)}</td>
              </tr>
              {open === entry.id && (
                <tr className="access-detail">
                  <td colSpan={5}>
                    <dl className="access-facts">
                      {(entry.close_reason || entry.detail) && (
                        <div>
                          <dt>Reason</dt>
                          <dd className="mono wrap">{entry.close_reason || entry.detail}</dd>
                        </div>
                      )}
                      <div>
                        <dt>Address</dt>
                        <dd className="mono wrap">
                          {entry.client}
                          {entry.forwarded_for && ` (forwarded for ${entry.forwarded_for})`} to {entry.host || "–"}
                        </dd>
                      </div>
                      <div>
                        <dt>Duration</dt>
                        <dd>
                          {entry.duration_ms === null ? "Still going" : entry.kind === "websocket" ? span(entry.duration_ms) : formatDuration(entry.duration_ms)}
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
                    </dl>
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
