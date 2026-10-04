import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Session } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { formatDate, formatNumber, lastTraffic } from "../format";
import { useRequests, useSessions, useStatus, useToast } from "../hooks";
import { ExpandRow } from "../components/ExpandRow";
import { RequestList } from "../components/RequestList";
import { groupRepeats, mergeRows } from "../traffic";
import { RouteBadge } from "../components/RouteBadge";

type Cleanup = { session: Session; remove: boolean } | null;

/** Most recently active first; sessions without traffic fall back to their start time. */
const byLastActivity = (a: Session, b: Session) =>
  (b.last_activity_at ?? b.started_at).localeCompare(a.last_activity_at ?? a.started_at);

export function SessionsPage() {
  const sessions = useSessions();
  const status = useStatus();
  const client = useQueryClient();
  const notify = useToast();
  const [cleanup, setCleanup] = useState<Cleanup>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const [open, setOpen] = useState<Set<string>>(() => new Set());
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
    mutationFn: api.endSession,
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
        <div className="section-head">
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
        <p className="lede">
          A session is one agent run. The launcher opens one for you; requests without a session get their own. Expand a
          session to see its requests and settings, or open it on its own page.
        </p>
      </header>
      {ordered.length ? (
        <ul className="xlist" aria-label="Sessions">
          {ordered.map((session) => {
            const inFlight = busy.has(session.session_id);
            const path = `/sessions/${encodeURIComponent(session.session_id)}`;
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
                    </span>
                    <span className="xrow-meta">
                      {inFlight ? (
                        <span className="pill live">
                          <span className="live-mark" aria-hidden="true" /> streaming
                        </span>
                      ) : session.ended_at ? (
                        <span className="subtle">{session.exit_status == null ? "Ended" : `Ended, exit ${session.exit_status}`}</span>
                      ) : (
                        <span className="good">Open</span>
                      )}
                      <span>
                        {formatNumber(session.request_count)} {session.request_count === 1 ? "request" : "requests"}
                      </span>
                      <span className="subtle" title={session.last_activity_at ? formatDate(session.last_activity_at) : undefined}>
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
        <p className="empty">No sessions yet.</p>
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
          This permanently removes every saved request, telemetry file and payload, and deletes the sessions. Sessions
          used in the last 15 minutes stay, empty, so agents that are still running keep working. Sessions with a request
          in flight are skipped. It can't be undone.
        </p>
        <p className="subtle">
          {formatNumber(ordered.length)} sessions, {formatNumber(ordered.reduce((total, item) => total + item.request_count, 0))} requests.
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
            <input type="checkbox" checked={session.trace} disabled={traceBusy} onChange={(event) => onTrace(event.target.checked)} />
            Capture full request and response payloads
          </label>
        </dd>
      </dl>
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
          <button type="button" onClick={onEnd}>
            End session
          </button>
        )}
        <button type="button" className="danger" disabled={inFlight} title={waitTitle} onClick={() => onCleanup(false)}>
          Clear data
        </button>
        <button type="button" className="danger" disabled={inFlight} title={waitTitle} onClick={() => onCleanup(true)}>
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
