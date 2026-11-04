import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type TerminalSummary } from "../api";
import { NotifyToggle } from "../components/Attention";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../hooks";
import { PartTag } from "../components/PartTag";
import { SharingBadge } from "../components/Sharing";
import { formatRelative } from "../format";

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

export function TerminalsPage() {
  // While it fails (offline, or the server restarting), keep asking so the page recovers by itself.
  const remote = useQuery({ queryKey: ["remote"], queryFn: api.remote, refetchInterval: (query) => (query.state.status === "error" ? 10000 : false) });
  const trusted = remote.data?.trusted === true;
  const terminals = useQuery({ queryKey: ["terminals"], queryFn: () => api.terminals(), refetchInterval: 3000, enabled: trusted });
  const [showEnded, setShowEnded] = useState(false);
  // The terminals to delete, with their sessions: one card's, or every disconnected one.
  const [removing, setRemoving] = useState<TerminalSummary[] | null>(null);
  const client = useQueryClient();
  const notify = useToast();
  const all = terminals.data?.terminals ?? [];
  const running = all.filter((item) => !(item.ended_at ?? item.session_ended_at)).sort(byRecent);
  const ended = all.filter((item) => item.ended_at ?? item.session_ended_at).sort(byRecent);
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

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Running agents</h1>
        <p className="lede">
          Agents started with <code>agent … --control</code> run in a terminal this server can see, from any machine
          signed in to it. Open one to read its screen and scrollback and to type into it, from any device.
        </p>
      </header>

      {remote.isError && <p className="bad">{remote.error.message}</p>}
      {remote.data && !trusted && <PairPanel />}

      {trusted && (
        <section className="section" aria-labelledby="running-heading">
          <div className="section-head">
            <h2 id="running-heading">Running now</h2>
            <div className="terminal-actions">
              {disconnected.length > 1 && (
                <button type="button" onClick={() => ask(disconnected)}>
                  Remove {disconnected.length} not connected
                </button>
              )}
              <NotifyToggle />
            </div>
          </div>
          {terminals.isError && <p className="bad">{terminals.error.message}</p>}
          {running.length ? (
            <ul className="terminal-list">
              {running.map((terminal) => (
                <TerminalCard key={terminal.session_id} terminal={terminal} onRemove={() => ask([terminal])} />
              ))}
            </ul>
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
                <ul className="terminal-list">
                  {ended.map((terminal) => (
                    <TerminalCard key={terminal.session_id} terminal={terminal} onRemove={() => ask([terminal])} />
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

function TerminalCard({ terminal, onRemove }: { terminal: TerminalSummary; onRemove: () => void }) {
  const state = terminalState(terminal);
  const path = `/terminals/${encodeURIComponent(terminal.session_id)}`;
  return (
    <li className={`terminal-card tone-${state.tone}`}>
      <div className="terminal-card-head">
        <Link to={path} className="strong-link">
          {terminal.agent_name}
          {terminal.project && <span className="subtle"> · {terminal.project}</span>}
        </Link>
        <span className={`pill ${state.tone === "idle" ? "" : state.tone}`}>{state.label}</span>
      </div>
      <p className={state.tone === "ask" ? "terminal-question" : "subtle"}>{state.detail}</p>
      <SharingBadge you={terminal.you} />
      <p className="terminal-card-meta mono subtle">
        {[terminal.machine_name, terminal.model, terminal.backend !== "direct" ? terminal.backend : "own account", `${terminal.request_count} requests`]
          .filter(Boolean)
          .join(" · ")}
      </p>
      <div className="terminal-actions">
        <Link to={path} className="button">
          Open terminal
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
