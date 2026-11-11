import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type TerminalSummary } from "../api";
import { NotifyToggle } from "../components/Attention";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../hooks";
import { PartTag } from "../components/PartTag";
import { SharingBadge } from "../components/Sharing";
import { formatNumber, formatRelative } from "../format";

/** Output within this long counts as the agent still drawing (a spinner, streamed text). */
const BUSY_MS = 5000;

export type TerminalState = { label: string; tone: "live" | "good" | "bad" | "idle" | "ask"; detail: string };

/** What a shared terminal is doing, from its connection, the proxy's traffic and how recently it printed. */
export function terminalState(terminal: TerminalSummary | undefined, now = Date.now()): TerminalState {
  if (!terminal) return { label: "Connecting", tone: "idle", detail: "" };
  const ended = terminal.ended_at ?? terminal.session_ended_at;
  if (ended) {
    const status = terminal.exit_status;
    return {
      label: status ? `Exited (${status})` : "Exited",
      tone: status ? "bad" : "idle",
      detail: `Ended ${formatRelative(ended)}`,
    };
  }
  if (!terminal.connected) {
    return { label: "Not connected", tone: "bad", detail: "The launcher isn't connected: it was closed, or is reconnecting." };
  }
  const quietFor = terminal.last_output_at ? now - new Date(terminal.last_output_at).getTime() : Infinity;
  // The viewer's socket brings output sooner than the polled list: newer output means it's working again.
  const attention = terminal.attention && (terminal.last_output_at ?? "") <= (terminal.attention.since ?? "") ? terminal.attention : null;
  if (attention?.kind === "question") {
    return { label: "Needs you", tone: "ask", detail: attention.question ?? "It's asking you something" };
  }
  if (attention?.kind === "idle") {
    return { label: "Waiting", tone: "good", detail: `Done for now, waiting for you (quiet for ${duration(quietFor)})` };
  }
  if (terminal.in_flight > 0) return { label: "Working", tone: "live", detail: "Waiting on the model" };
  if (quietFor < BUSY_MS) return { label: "Working", tone: "live", detail: "Printing output" };
  return { label: "Waiting", tone: "good", detail: `No output for ${duration(quietFor)}; it may be waiting for you` };
}

function duration(ms: number) {
  if (!Number.isFinite(ms)) return "a while";
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.round(minutes / 60)} h`;
}

/** A terminal whose launcher is gone, which you may delete with its session. */
const removable = (terminal: TerminalSummary) => !terminal.connected && terminal.in_flight === 0 && terminal.you?.control === true;

const byRecent = (a: TerminalSummary, b: TerminalSummary) =>
  (b.last_output_at ?? b.started_at ?? "").localeCompare(a.last_output_at ?? a.started_at ?? "");

/** The states a running agent can be filtered by, most urgent first; the key is the state's tone. */
const FILTERS: { tone: TerminalState["tone"]; label: string }[] = [
  { tone: "ask", label: "Needs you" },
  { tone: "live", label: "Working" },
  { tone: "good", label: "Waiting" },
  { tone: "bad", label: "Not connected" },
];

/** Who started it: you, or the person the server names. Unknown without accounts on the session. */
const startedBy = (terminal: TerminalSummary) => (terminal.you ? (terminal.you.owner ? "You" : terminal.you.sharing.owner ?? "") : "");

export function TerminalsPage() {
  // While it fails (offline, or the server restarting), keep asking so the page recovers by itself.
  const remote = useQuery({ queryKey: ["remote"], queryFn: api.remote, refetchInterval: (query) => (query.state.status === "error" ? 10000 : false) });
  const trusted = remote.data?.trusted === true;
  const terminals = useQuery({ queryKey: ["terminals"], queryFn: () => api.terminals(), refetchInterval: 3000, enabled: trusted });
  const [showEnded, setShowEnded] = useState(false);
  const [query, setQuery] = useState("");
  const [show, setShow] = useState<TerminalState["tone"] | "">("");
  // The terminals to delete, with their sessions: one row's, or every disconnected one.
  const [removing, setRemoving] = useState<TerminalSummary[] | null>(null);
  const client = useQueryClient();
  const notify = useToast();
  const all = terminals.data?.terminals ?? [];
  const running = all.filter((item) => !(item.ended_at ?? item.session_ended_at)).sort(byRecent);
  const disconnected = all.filter(removable);
  const remove = useMutation({
    mutationFn: async (targets: TerminalSummary[]) => {
      const results = await Promise.allSettled(targets.map((terminal) => api.deleteSession(terminal.session_id)));
      const failed = results.filter((result) => result.status === "rejected").length;
      if (failed === targets.length) throw (results[0] as PromiseRejectedResult).reason;
      return { removed: targets.length - failed, failed };
    },
    onSuccess: ({ removed, failed }) => {
      notify(`Removed ${removed} ${removed === 1 ? "session" : "sessions"}${failed ? `; ${failed} couldn't be removed` : ""}`);
      setRemoving(null);
      void client.invalidateQueries();
    },
  });
  const ask = (targets: TerminalSummary[]) => {
    remove.reset();
    setRemoving(targets);
  };

  // The search narrows both lists; the state chips count within it.
  const text = query.trim().toLowerCase();
  const matches = (terminal: TerminalSummary) =>
    !text ||
    [terminal.agent_name, terminal.agent, terminal.project, terminal.project_path, startedBy(terminal), terminal.machine_name, terminal.model, terminal.backend, terminal.session_id].some(
      (value) => (value ?? "").toLowerCase().includes(text),
    );
  const now = Date.now();
  const found = running.filter(matches).map((terminal) => ({ terminal, state: terminalState(terminal, now) }));
  const ended = all.filter((item) => item.ended_at ?? item.session_ended_at).filter(matches).sort(byRecent);
  const counts = new Map<string, number>();
  for (const { state } of found) counts.set(state.tone, (counts.get(state.tone) ?? 0) + 1);
  const shown = show ? found.filter(({ state }) => state.tone === show) : found;
  const tones = running.map((terminal) => terminalState(terminal, now).tone);

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Running agents</h1>
        <p className="lede">
          The agents running now with a shared terminal: who started each, what it's doing and how much it has sent. Open one
          to read its screen and type into it, from any device.
        </p>
      </header>

      {remote.isError && <p className="bad">{remote.error.message}</p>}
      {remote.data && !trusted && <PairPanel />}

      {trusted && running.length > 0 && (
        <dl className="stat-cards" aria-label="Running agents">
          <div>
            <dt>Running</dt>
            <dd>{formatNumber(running.length)}</dd>
          </div>
          <div>
            <dt>Need you</dt>
            <dd>{formatNumber(tones.filter((tone) => tone === "ask").length)}</dd>
          </div>
          <div>
            <dt>Working</dt>
            <dd>{formatNumber(tones.filter((tone) => tone === "live").length)}</dd>
          </div>
          <div>
            <dt>Requests</dt>
            <dd>
              {formatNumber(running.reduce((total, item) => total + item.request_count, 0))}{" "}
              {running.some((item) => item.in_flight > 0) && <small>{running.reduce((total, item) => total + item.in_flight, 0)} in flight</small>}
            </dd>
          </div>
        </dl>
      )}

      {trusted && (
        <section className="section" aria-label="Running now">
          {terminals.isError && <p className="bad">{terminals.error.message}</p>}
          {all.length > 0 && (
            <>
              <div className="filters">
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search agent, project, person, machine or model"
                  aria-label="Search running agents"
                />
              </div>
              <div className="chip-bar" role="group" aria-label="Agent state">
                <button type="button" aria-pressed={!show} onClick={() => setShow("")}>
                  All <span className="count">{formatNumber(found.length)}</span>
                </button>
                {FILTERS.filter(({ tone }) => counts.get(tone)).map(({ tone, label }) => (
                  <button key={tone} type="button" aria-pressed={show === tone} onClick={() => setShow(show === tone ? "" : tone)}>
                    {label} <span className="count">{formatNumber(counts.get(tone))}</span>
                  </button>
                ))}
                <span className="chip-gap" aria-hidden="true" />
                {disconnected.length > 1 && (
                  <button type="button" onClick={() => ask(disconnected)}>
                    Remove {disconnected.length} not connected
                  </button>
                )}
                <NotifyToggle />
              </div>
            </>
          )}
          {shown.length ? (
            <ul className="agent-list" aria-label="Running agents">
              {shown.map(({ terminal, state }) => (
                <AgentRow key={terminal.session_id} terminal={terminal} state={state} onRemove={() => ask([terminal])} />
              ))}
            </ul>
          ) : running.length ? (
            <p className="empty">No running agent matches.</p>
          ) : (
            terminals.isSuccess && (
              <div className="empty panel">
                <p>No agents are sharing their terminal.</p>
                <p className="subtle">
                  Start one with <code>--control</code> from your project folder, for example{" "}
                  <code>agent claude-account . --control</code>; it shows up here while it runs.{" "}
                  <Link to="/agents">Start an agent</Link>
                </p>
              </div>
            )
          )}
          {ended.length > 0 && (
            <>
              <button type="button" className="link" aria-expanded={showEnded} onClick={() => setShowEnded(!showEnded)}>
                {showEnded ? "Hide" : "Show"} {ended.length} finished {ended.length === 1 ? "session" : "sessions"}
              </button>
              {showEnded && (
                <ul className="agent-list" aria-label="Finished sessions">
                  {ended.map((terminal) => (
                    <AgentRow key={terminal.session_id} terminal={terminal} state={terminalState(terminal, now)} onRemove={() => ask([terminal])} />
                  ))}
                </ul>
              )}
            </>
          )}
        </section>
      )}

      <ConfirmDialog
        open={removing !== null}
        title={removing?.length === 1 ? "Remove this session?" : `Remove ${removing?.length ?? 0} sessions?`}
        confirmLabel="Remove"
        busy={remove.isPending}
        error={remove.error?.message ?? null}
        onConfirm={() => removing && remove.mutate(removing)}
        onCancel={() => setRemoving(null)}
      >
        <p>
          {removing?.length === 1
            ? `${removing[0].agent_name}${removing[0].project ? ` in ${removing[0].project}` : ""} isn't connected.`
            : "These agents' terminals aren't connected."}{" "}
          Removing deletes {removing?.length === 1 ? "the session" : "the sessions"} with everything recorded: the terminal output,
          requests, traces and chats. It can't be undone.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/**
 * One agent: what it is and its state, what it's doing, then who started it, where, with which
 * model and how much it has sent. The row opens its terminal; its session is the link beside it.
 */
function AgentRow({ terminal, state, onRemove }: { terminal: TerminalSummary; state: TerminalState; onRemove: () => void }) {
  const id = encodeURIComponent(terminal.session_id);
  const who = startedBy(terminal);
  const provider = terminal.backend !== "direct" ? terminal.backend : "own account";
  return (
    <li className={`agent-row tone-${state.tone}`}>
      <Link to={`/terminals/${id}`} className="agent-row-main" title="Open its terminal">
        <span className="agent-row-line">
          <span className={`state-dot ${state.tone}`} aria-hidden="true" />
          <strong>{terminal.agent_name}</strong>
          {terminal.project && (
            <span className="subtle agent-row-project" title={terminal.project_path ?? undefined}>
              in {terminal.project}
            </span>
          )}
          <SharingBadge you={terminal.you} />
          <span className="agent-row-end">
            <span className={`pill ${state.tone === "idle" ? "" : state.tone}`}>{state.label}</span>
          </span>
        </span>
        {state.detail && <span className={`agent-row-doing ${state.tone === "ask" ? "terminal-question" : "subtle"}`}>{state.detail}</span>}
        <span className="agent-row-line agent-row-meta">
          {who && <span>Started by {who === "You" ? "you" : who}</span>}
          {terminal.machine_name && <span>on {terminal.machine_name}</span>}
          <span className="mono">{[terminal.model, provider].filter(Boolean).join(" · ")}</span>
          {terminal.started_at && <span title={new Date(terminal.started_at).toLocaleString()}>started {formatRelative(terminal.started_at)}</span>}
          <span className="agent-row-end">
            {formatNumber(terminal.request_count)} {terminal.request_count === 1 ? "request" : "requests"}
            {terminal.in_flight > 0 && `, ${terminal.in_flight} in flight`}
            {terminal.last_activity_at && ` · last ${formatRelative(terminal.last_activity_at)}`}
          </span>
        </span>
      </Link>
      <div className="agent-row-actions">
        <Link to={`/sessions/${id}`} className="button">
          Session
        </Link>
        {removable(terminal) && (
          <button type="button" className="danger" onClick={onRemove}>
            Remove
          </button>
        )}
      </div>
    </li>
  );
}

export function PairPanel() {
  return (
    <section className="panel" aria-labelledby="pair-heading">
      <div className="panel-head">
        <h2 id="pair-heading">Sign in again</h2>
        <p className="subtle">
          Your sign-in has expired or you no longer have permission. Sign in through the authentication service.
        </p>
      </div>
      <button type="button" className="primary" onClick={() => api.login()}>Sign in</button>
    </section>
  );
}
