import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Session } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { formatDate, formatNumber, formatRelative, lastTraffic } from "../format";
import { useSessions, useStatus, useToast } from "../hooks";

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

  return (
    <div className="page">
      <header className="page-head">
        <h1>Sessions</h1>
        <p className="lede">
          A session is one agent run. The launcher opens one for you; requests without a session get their own.
        </p>
      </header>
      {sessions.data?.length ? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Model</th>
                <th scope="col">Last traffic</th>
                <th scope="col" className="num">Requests</th>
                <th scope="col">Payloads</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {[...sessions.data].sort(byLastActivity).map((session) => {
                const inFlight = busy.has(session.session_id);
                return (
                  <tr key={session.session_id}>
                    <td>
                      <Link className="strong-link" to={`/sessions/${encodeURIComponent(session.session_id)}`}>
                        {session.client}
                      </Link>
                      {session.project && <span className="subtle"> in {session.project}</span>}
                      <small className="mono">{session.session_id}</small>
                      <small>
                        {session.ended_at ? (
                          session.exit_status == null ? "Ended" : `Ended with exit code ${session.exit_status}`
                        ) : (
                          <span className="good">Open</span>
                        )}
                      </small>
                    </td>
                    <td>
                      {session.model || "Set by agent"}
                      <small>{session.backend}</small>
                    </td>
                    <td title={session.last_activity_at ? formatDate(session.last_activity_at) : undefined}>
                      {inFlight ? <span className="warn">Streaming now</span> : lastTraffic(session)}
                      <small title={formatDate(session.started_at)}>Started {formatRelative(session.started_at)}</small>
                    </td>
                    <td className="num">{formatNumber(session.request_count)}</td>
                    <td>
                      <label className="check">
                        <input
                          type="checkbox"
                          checked={session.trace}
                          disabled={trace.isPending}
                          onChange={(event) => trace.mutate({ id: session.session_id, enabled: event.target.checked })}
                        />
                        Capture
                      </label>
                    </td>
                    <td>
                      <div className="row wrap">
                        <Link className="button" to={`/sessions/${encodeURIComponent(session.session_id)}`}>
                          Watch
                        </Link>
                        <Link className="button" to={`/requests?session=${encodeURIComponent(session.session_id)}`}>
                          Review
                        </Link>
                        <a className="button" href={api.telemetryUrl(session.session_id)}>
                          JSONL
                        </a>
                        {!session.ended_at && (
                          <button type="button" onClick={() => end.mutate(session.session_id)}>
                            End
                          </button>
                        )}
                        <button
                          type="button"
                          className="danger"
                          disabled={inFlight}
                          title={inFlight ? "Wait for in-flight requests to finish" : undefined}
                          onClick={() => {
                            remove.reset();
                            setCleanup({ session, remove: false });
                          }}
                        >
                          Clear data
                        </button>
                        <button
                          type="button"
                          className="danger"
                          disabled={inFlight}
                          title={inFlight ? "Wait for in-flight requests to finish" : undefined}
                          onClick={() => {
                            remove.reset();
                            setCleanup({ session, remove: true });
                          }}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">No sessions yet.</p>
      )}

      <NewSession />

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
