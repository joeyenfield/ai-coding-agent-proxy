import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Session, type SessionUsage } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { formatCompact, formatDate, formatNumber, formatUsd, lastTraffic } from "../format";
import { useRequests, useSessions, useStatus, useToast } from "../hooks";
import { ExpandRow } from "../components/ExpandRow";
import { RequestList } from "../components/RequestList";
import { groupRepeats, mergeRows } from "../traffic";
import { RouteBadge } from "../components/RouteBadge";
import { PartTag } from "../components/PartTag";
import { SharingBadge, SharingControl } from "../components/Sharing";

type Cleanup = { session: Session; remove: boolean } | null;

/** What a session is doing: a request in flight, open and idle, or ended. */
type SessionState = "streaming" | "open" | "ended";
const STATE_LABELS: Record<SessionState, string> = { streaming: "Streaming", open: "Open", ended: "Ended" };

/** Most recently active first; sessions without traffic fall back to their start time. */
const byLastActivity = (a: Session, b: Session) =>
  (b.last_activity_at ?? b.started_at).localeCompare(a.last_activity_at ?? a.started_at);

/** What the list is ordered by: the latest traffic, or what the sessions used, the most first. */
type SessionSort = "recent" | "cost" | "tokens" | "requests";
const SORT_LABELS: Record<SessionSort, string> = { recent: "Latest", cost: "Cost", tokens: "Tokens", requests: "Requests" };

/** A session's estimated cost: money spent on paid APIs and a subscription's calls at API prices together. */
const costOf = (usage: SessionUsage | undefined) => (usage ? usage.usd + usage.subscription_usd : 0);
const tokensOf = (usage: SessionUsage | undefined) => (usage ? usage.input_tokens + usage.output_tokens : 0);

/** What a row's cost is made of, for its tooltip. */
function costTitle(used: SessionUsage | undefined) {
  if (!used || used.priced === 0) return "No estimated cost: no model call of this session has a price";
  const parts = [];
  if (used.usd > 0) parts.push(`${formatUsd(used.usd)} spent on paid APIs`);
  if (used.subscription_usd > 0) parts.push(`${formatUsd(used.subscription_usd)} at API prices, on a subscription (not billed)`);
  if (used.priced < used.model_calls) parts.push(`${formatNumber(used.model_calls - used.priced)} model calls have no cost`);
  return parts.join(". ");
}

export function SessionsPage() {
  const sessions = useSessions();
  const status = useStatus();
  const client = useQueryClient();
  const notify = useToast();
  const [cleanup, setCleanup] = useState<Cleanup>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState("");
  const [show, setShow] = useState<SessionState | "">("");
  const [sort, setSort] = useState<SessionSort>("recent");
  // Tokens and cost of every session, summed by the server: the list's columns.
  const usage = useQuery({ queryKey: ["sessions-usage"], queryFn: api.sessionsUsage, refetchInterval: 30_000, retry: false }).data ?? {};
  const busy = new Set(status.data?.current_requests.map((item) => item.session_id));
  const refresh = () => client.invalidateQueries();

  const trace = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api.setTrace(id, enabled),
    onSuccess: (_, { enabled }) => {
      notify(enabled ? "Tracing enabled for future requests" : "Tracing disabled");
      refresh();
    },
    onError: (error) => notify(error.message),
  });
  const end = useMutation({
    mutationFn: (sessionId: string) => api.endSession(sessionId),
    onSuccess: () => {
      notify("Session ended");
      refresh();
    },
    onError: (error) => notify(error.message),
  });
  const removeAll = useMutation({
    mutationFn: api.deleteAllSessions,
    onSuccess: (result) => {
      const parts = [`${result.deleted} deleted`];
      if (result.cleared) parts.push(`${result.cleared} recently active ${result.cleared === 1 ? "session" : "sessions"} kept but emptied`);
      if (result.skipped.length) parts.push(`${result.skipped.length} skipped while streaming`);
      notify(`Sessions: ${parts.join(", ")}`);
      setConfirmAll(false);
      setOpen(new Set());
      refresh();
    },
  });
  const remove = useMutation({
    mutationFn: async ({ session, remove }: { session: Session; remove: boolean }) => {
      if (remove) await api.deleteSession(session.session_id);
      else await api.clearSession(session.session_id);
    },
    onSuccess: (_, { remove }) => {
      notify(remove ? "Session deleted" : "Session data cleared");
      setCleanup(null);
      refresh();
    },
  });

  const ordered = [...(sessions.data ?? [])].sort(byLastActivity);
  const stateOf = (session: Session): SessionState => (busy.has(session.session_id) ? "streaming" : session.ended_at ? "ended" : "open");
  const text = query.trim().toLowerCase();
  // The search narrows the list; the status chips count within it.
  const found = ordered.filter(
    (session) =>
      !text ||
      [session.client, session.project ?? "", session.model ?? "", session.backend, session.session_id].some((value) => value.toLowerCase().includes(text)),
  );
  const counts = { streaming: 0, open: 0, ended: 0 };
  for (const session of found) counts[stateOf(session)] += 1;
  const filtered = show ? found.filter((session) => stateOf(session) === show) : found;
  const measure: Record<Exclude<SessionSort, "recent">, (session: Session) => number> = {
    cost: (session) => costOf(usage[session.session_id]),
    tokens: (session) => tokensOf(usage[session.session_id]),
    requests: (session) => session.request_count,
  };
  // A stable sort: sessions that used the same keep the latest first.
  const shown = sort === "recent" ? filtered : [...filtered].sort((a, b) => measure[sort](b) - measure[sort](a));
  const totals = ordered.reduce(
    (sum, session) => {
      const item = usage[session.session_id];
      return item ? { tokens: sum.tokens + tokensOf(item), usd: sum.usd + item.usd, subscription_usd: sum.subscription_usd + item.subscription_usd } : sum;
    },
    { tokens: 0, usd: 0, subscription_usd: 0 },
  );
  // Each row's cost bar is to the scale of the costliest session shown.
  const topCost = Math.max(...shown.map((session) => costOf(usage[session.session_id])), 0);
  const totalRequests = ordered.reduce((total, item) => total + item.request_count, 0);
  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <div className="page-title">
          <h1>Sessions</h1>
          {ordered.length > 0 && (
            <button
              type="button"
              className="danger"
              onClick={() => {
                removeAll.reset();
                setConfirmAll(true);
              }}
            >
              Delete all sessions…
            </button>
          )}
        </div>
        <p className="lede">A session is one agent run. Expand one for its settings and latest requests, or open it on its own page.</p>
      </header>
      {ordered.length > 0 && (
        <dl className="stat-cards" aria-label="Sessions">
          <div>
            <dt>Sessions</dt>
            <dd>{formatNumber(ordered.length)}</dd>
          </div>
          <div>
            <dt>Streaming now</dt>
            <dd>{formatNumber(ordered.filter((session) => busy.has(session.session_id)).length)}</dd>
          </div>
          <div>
            <dt>Open</dt>
            <dd>{formatNumber(ordered.filter((session) => !session.ended_at).length)}</dd>
          </div>
          <div>
            <dt>Requests</dt>
            <dd>{formatNumber(totalRequests)}</dd>
          </div>
          <div>
            <dt>Tokens</dt>
            <dd>{formatCompact(totals.tokens)}</dd>
          </div>
          <div>
            <dt>Cost</dt>
            <dd title="Estimated: money spent on paid APIs, and what subscriptions' calls would cost at API prices, which isn't billed">
              {totals.usd > 0 || totals.subscription_usd === 0 ? formatUsd(totals.usd) : `≈ ${formatUsd(totals.subscription_usd)}`}{" "}
              {totals.usd > 0 && totals.subscription_usd > 0 && <small>+ ≈ {formatUsd(totals.subscription_usd)}</small>}
            </dd>
          </div>
        </dl>
      )}
      {ordered.length ? (
        <section className="session-list">
          <div className="filters">
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search agent, project, model or session id"
              aria-label="Search sessions"
            />
          </div>
          <div className="chip-bar" role="group" aria-label="Session status">
            <button type="button" aria-pressed={!show} onClick={() => setShow("")}>
              All <span className="count">{formatNumber(found.length)}</span>
            </button>
            {(Object.keys(STATE_LABELS) as SessionState[])
              .filter((name) => counts[name])
              .map((name) => (
                <button key={name} type="button" aria-pressed={show === name} onClick={() => setShow(show === name ? "" : name)}>
                  {STATE_LABELS[name]} <span className="count">{formatNumber(counts[name])}</span>
                </button>
              ))}
            <span className="chip-gap" />
            <span className="subtle">Sort by</span>
            {(Object.keys(SORT_LABELS) as SessionSort[]).map((name) => (
              <button key={name} type="button" aria-pressed={sort === name} onClick={() => setSort(name)}>
                {SORT_LABELS[name]}
              </button>
            ))}
          </div>
          {shown.length > 0 && (
            // The columns' names, laid out as a row is so they sit above its figures.
            <div className="xrow-head session-columns" aria-hidden="true">
              <span className="xrow-toggle">
                <span className="xrow-caret">▸</span>
                <span className="xrow-summary">
                  <span className="xrow-main">Session</span>
                  <span className="xrow-meta session-cols">
                    <span className="session-state">Status</span>
                    <span className="session-count">Requests</span>
                    <span className="session-tokens">Tokens in</span>
                    <span className="session-tokens">Tokens out</span>
                    <span className="session-cost">Cost</span>
                    <span className="session-when">Last traffic</span>
                  </span>
                </span>
              </span>
              <span className="xrow-actions">
                <span className="button">Open</span>
              </span>
            </div>
          )}
          {shown.length ? (
            <ul className="xlist" aria-label="Sessions">
              {shown.map((session) => {
                const state = stateOf(session);
                const inFlight = state === "streaming";
                const path = `/sessions/${encodeURIComponent(session.session_id)}`;
                const used = usage[session.session_id];
                const cost = costOf(used);
                return (
                  <ExpandRow
                    key={session.session_id}
                    expanded={open.has(session.session_id)}
                    onToggle={() => toggle(session.session_id)}
                    tone={inFlight ? "live" : undefined}
                    summary={
                      <>
                        <span className="xrow-main">
                          <strong>{session.client}</strong>
                          {session.project && <span className="subtle">in {session.project}</span>}
                          <RouteBadge item={session} />
                          {session.model && <span className="pill accent">{session.model}</span>}
                          <SharingBadge you={session.you} />
                        </span>
                        <span className="xrow-meta session-cols">
                          <span className="session-state">
                            {inFlight ? (
                              <span className="pill live">
                                <span className="live-mark" aria-hidden="true" /> Streaming
                              </span>
                            ) : state === "ended" ? (
                              <span className="pill" title={session.exit_status == null ? undefined : `Exit status ${session.exit_status}`}>
                                {session.exit_status ? `Ended, exit ${session.exit_status}` : "Ended"}
                              </span>
                            ) : (
                              <span className="pill good">Open</span>
                            )}
                          </span>
                          <span className="session-count" title={used ? `${formatNumber(used.model_calls)} of them model calls that used tokens` : undefined}>
                            {formatNumber(session.request_count)}
                            <span className="session-unit"> {session.request_count === 1 ? "request" : "requests"}</span>
                          </span>
                          <span className={used ? "session-tokens" : "session-tokens is-empty"} title={used ? `${formatNumber(used.input_tokens)} tokens in` : undefined}>
                            {used ? formatCompact(used.input_tokens) : "–"}
                            <span className="session-unit"> in</span>
                          </span>
                          <span className={used ? "session-tokens" : "session-tokens is-empty"} title={used ? `${formatNumber(used.output_tokens)} tokens out` : undefined}>
                            {used ? formatCompact(used.output_tokens) : "–"}
                            <span className="session-unit"> out</span>
                          </span>
                          <span className={used && used.priced > 0 ? "session-cost" : "session-cost is-empty"} title={costTitle(used)}>
                            {!used || used.priced === 0 ? "–" : used.usd > 0 ? formatUsd(cost) : `≈ ${formatUsd(cost)}`}
                            <span className="session-cost-bar" aria-hidden="true">
                              <span style={{ width: `${topCost > 0 ? (cost / topCost) * 100 : 0}%` }} />
                            </span>
                          </span>
                          <span className="session-when subtle" title={session.last_activity_at ? formatDate(session.last_activity_at) : undefined}>
                            {lastTraffic(session)}
                          </span>
                        </span>
                      </>
                    }
                    actions={
                      <Link className="button" to={path}>
                        Open
                      </Link>
                    }
                  >
                    <SessionBody
                      session={session}
                      inFlight={inFlight}
                      onTrace={(enabled) => trace.mutate({ id: session.session_id, enabled })}
                      traceBusy={trace.isPending}
                      onEnd={() => end.mutate(session.session_id)}
                      onCleanup={(removeSession) => {
                        remove.reset();
                        setCleanup({ session, remove: removeSession });
                      }}
                    />
                  </ExpandRow>
                );
              })}
            </ul>
          ) : (
            <p className="empty">No session matches.</p>
          )}
        </section>
      ) : (
        <p className="empty">No sessions yet. Start an agent and its session shows here.</p>
      )}

      <NewSession />

      <ConfirmDialog
        open={confirmAll}
        title="Delete all sessions?"
        confirmLabel="Delete everything"
        busy={removeAll.isPending}
        error={removeAll.error?.message}
        onCancel={() => setConfirmAll(false)}
        onConfirm={() => removeAll.mutate()}
      >
        <p>
          This permanently removes every saved request, payload and terminal recording, and deletes the sessions. Sessions
          used in the last 15 minutes stay, empty, so agents that are still running keep working. Sessions with a request
          in flight are skipped. It can't be undone.
        </p>
        <p className="subtle">
          {formatNumber(ordered.length)} sessions, {formatNumber(totalRequests)} requests.
        </p>
      </ConfirmDialog>

      <ConfirmDialog
        open={cleanup != null}
        title={cleanup?.remove ? "Delete this session?" : "Clear this session's data?"}
        confirmLabel={cleanup?.remove ? "Delete session" : "Clear data"}
        busy={remove.isPending}
        error={remove.error?.message}
        onCancel={() => setCleanup(null)}
        onConfirm={() => cleanup && remove.mutate(cleanup)}
      >
        <p>
          {cleanup?.remove
            ? `This permanently deletes the ${cleanup.session.client} session with its saved requests, telemetry and payloads.`
            : `This permanently removes the saved requests, telemetry and payloads from the ${cleanup?.session.client} session. The session and its capture setting stay.`}{" "}
          It can't be undone.
        </p>
        <p className="mono subtle">{cleanup?.session.session_id}</p>
      </ConfirmDialog>
    </div>
  );
}

/** What an expanded session row shows: its settings, actions and latest requests. */
function SessionBody({
  session,
  inFlight,
  onTrace,
  traceBusy,
  onEnd,
  onCleanup,
}: {
  session: Session;
  inFlight: boolean;
  onTrace: (enabled: boolean) => void;
  traceBusy: boolean;
  onEnd: () => void;
  onCleanup: (remove: boolean) => void;
}) {
  const requests = useRequests();
  const rows = useMemo(
    () => groupRepeats(mergeRows((requests.data ?? []).filter((record) => record.session_id === session.session_id), [])),
    [requests.data, session.session_id],
  );
  const waitTitle = inFlight ? "Wait for in-flight requests to finish" : undefined;
  // Shared to watch only: its owner (or someone they let in) changes it.
  const locked = Boolean(session.you && !session.you.control);
  return (
    <div className="session-body">
      <dl className="endpoints">
        <dt>Session</dt>
        <dd className="mono">{session.session_id}</dd>
        <dt>Started</dt>
        <dd>{formatDate(session.started_at)}</dd>
        <dt>Payloads</dt>
        <dd>
          <label className="check">
            <input type="checkbox" checked={session.trace} disabled={traceBusy || locked} onChange={(event) => onTrace(event.target.checked)} />
            Capture full request and response payloads
          </label>
        </dd>
      </dl>
      <SharingControl sessionId={session.session_id} you={session.you} />
      <div className="row wrap">
        <Link className="button" to={`/requests?session=${encodeURIComponent(session.session_id)}`}>
          Review requests
        </Link>
        <Link className="button" to={`/traffic?session=${encodeURIComponent(session.session_id)}`}>
          Traffic
        </Link>
        <a className="button" href={api.telemetryUrl(session.session_id)}>
          Download JSONL
        </a>
        {!session.ended_at && (
          <button type="button" onClick={onEnd} disabled={locked}>
            End session
          </button>
        )}
        <button type="button" className="danger" disabled={inFlight || locked} title={waitTitle} onClick={() => onCleanup(false)}>
          Clear data
        </button>
        <button type="button" className="danger" disabled={inFlight || locked} title={waitTitle} onClick={() => onCleanup(true)}>
          Delete
        </button>
      </div>
      <h3>Latest requests</h3>
      {rows.length ? <RequestList rows={rows} limit={5} /> : <p className="subtle">No requests recorded yet.</p>}
    </div>
  );
}

function NewSession() {
  const status = useStatus();
  const client = useQueryClient();
  const [created, setCreated] = useState<Session | null>(null);
  const create = useMutation({
    mutationFn: api.createSession,
    onSuccess: (session) => {
      setCreated(session);
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    create.mutate({
      client: String(data.get("client") || "manual"),
      model: String(data.get("model") || "") || null,
      backend: String(data.get("backend")),
      trace: data.has("trace"),
    });
  };
  const backends = Object.keys(status.data?.backends ?? {});
  return (
    <section className="section">
      <h2>Open a session manually</h2>
      <p className="subtle">
        For tools the launcher doesn't know. Point them at the session URL shown after you open it.
      </p>
      <form className="form-grid" onSubmit={submit}>
        <label className="field">
          Agent name
          <input name="client" defaultValue="manual" required />
        </label>
        <label className="field">
          Model
          <input name="model" placeholder="Chosen by the agent" />
        </label>
        <label className="field">
          Backend
          <select name="backend" key={status.data?.default_backend} defaultValue={status.data?.default_backend}>
            {backends.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <label className="check">
          <input name="trace" type="checkbox" />
          Capture payloads
        </label>
        <div className="row">
          <button className="primary" disabled={create.isPending}>
            Open session
          </button>
        </div>
      </form>
      {create.isError && <p className="bad">{create.error.message}</p>}
      {created && status.data && (
        <dl className="endpoints">
          <dt>OpenAI</dt>
          <dd className="mono">{`${status.data.proxy_url}/session/${created.session_id}/v1`}</dd>
          <dt>Anthropic</dt>
          <dd className="mono">{`${status.data.proxy_url}/session/${created.session_id}/anthropic`}</dd>
          <dt>Ollama</dt>
          <dd className="mono">{`${status.data.proxy_url}/session/${created.session_id}`}</dd>
        </dl>
      )}
    </section>
  );
}
