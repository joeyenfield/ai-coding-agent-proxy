import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { renderSVG } from "uqr";
import { api, type RemoteAddress, type RemoteStatus, type TerminalSummary } from "../api";
import { NotifyToggle } from "../components/Attention";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PartTag } from "../components/PartTag";
import { SharingBadge } from "../components/Sharing";
import { formatRelative } from "../format";
import { useClipboard, useToast } from "../hooks";

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

const byRecent = (a: TerminalSummary, b: TerminalSummary) =>
  (b.last_output_at ?? b.started_at ?? "").localeCompare(a.last_output_at ?? a.started_at ?? "");

export function TerminalsPage() {
  const remote = useQuery({ queryKey: ["remote"], queryFn: api.remote });
  const trusted = remote.data?.trusted === true;
  const terminals = useQuery({ queryKey: ["terminals"], queryFn: api.terminals, refetchInterval: 3000, enabled: trusted });
  const [showEnded, setShowEnded] = useState(false);
  const all = terminals.data?.terminals ?? [];
  const running = all.filter((item) => !(item.ended_at ?? item.session_ended_at)).sort(byRecent);
  const ended = all.filter((item) => item.ended_at ?? item.session_ended_at).sort(byRecent);

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
            <NotifyToggle />
          </div>
          {terminals.isError && <p className="bad">{terminals.error.message}</p>}
          {running.length ? (
            <ul className="terminal-list">
              {running.map((terminal) => (
                <TerminalCard key={terminal.session_id} terminal={terminal} />
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
                    <TerminalCard key={terminal.session_id} terminal={terminal} />
                  ))}
                </ul>
              )}
            </>
          )}
        </section>
      )}

      {remote.data?.local && <PhonePanel status={remote.data} />}
    </div>
  );
}

function TerminalCard({ terminal }: { terminal: TerminalSummary }) {
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
      <Link to={path} className="button">
        Open terminal
      </Link>
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

const KIND_LABELS: Record<RemoteAddress["kind"], string> = {
  configured: "proxy.public_url, works from any network",
  lan: "your network",
  tailscale: "Tailscale, works away from home",
  public: "public address",
  virtual: "virtual adapter, likely not reachable from a phone",
};

/** On this computer only: the QR code and link that pair a phone. */
function PhonePanel({ status }: { status: RemoteStatus }) {
  const client = useQueryClient();
  const copy = useClipboard();
  const notify = useToast();
  const addresses = status.addresses ?? [];
  const [chosen, setChosen] = useState(0);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const address = addresses[Math.min(chosen, addresses.length - 1)];
  const link = address && status.key ? `${address.url}/pair#${status.key}` : "";
  const qr = useMemo(() => (link ? renderSVG(link, { border: 2, ecc: "M" }) : ""), [link]);
  const code = useMutation({ mutationFn: api.pairingCode });
  const replace = useMutation({
    mutationFn: api.replaceRemoteKey,
    onSuccess: () => {
      notify("Access key replaced. Paired devices need the new code.");
      code.reset();
      setConfirmReplace(false);
      client.invalidateQueries({ queryKey: ["remote"] });
    },
  });

  return (
    <section className="panel" aria-labelledby="phone-heading">
      <div className="panel-head">
        <h2 id="phone-heading">Use it from your phone</h2>
        <p className="subtle">
          Pair a phone once and it can open this page, watch each agent's terminal and type into it. Nothing goes
          through a cloud service: the phone talks to this computer directly.
        </p>
      </div>
      {addresses.length === 0 ? (
        <div className="callout">
          <p>
            The proxy only accepts connections from this computer ({status.listen_host}), so a phone can't reach it. In{" "}
            <Link to="/settings">Settings</Link>, set the listen host to <code>0.0.0.0</code> and restart the proxy.
          </p>
          <p className="subtle">
            Other devices on your network can then open the console's other pages too. Terminals stay limited to paired
            devices.
          </p>
        </div>
      ) : (
        <>
          <ol className="steps">
            <li>
              The phone has to reach this computer: on your own network, through Tailscale, or at a public URL set as{" "}
              <code>proxy.public_url</code>, which works from any network.
            </li>
            <li>Open the code below and scan it with the phone's camera. If you can't, get a short code and type it on the phone.</li>
            <li>The phone opens Running agents, paired. Add it to the home screen to come back quickly.</li>
          </ol>
          {addresses.length > 1 && (
            <label className="field pair-address">
              Address the phone uses
              <select value={chosen} onChange={(event) => setChosen(Number(event.target.value))}>
                {addresses.map((item, index) => (
                  <option key={item.address} value={index}>
                    {item.address} ({[item.interface, KIND_LABELS[item.kind]].filter(Boolean).join(", ")})
                  </option>
                ))}
              </select>
            </label>
          )}
          <details className="pair-code">
            <summary>Show the pairing code</summary>
            <p className="subtle">Anyone who scans this code or copies the link can type into your agents. Keep it to yourself.</p>
            <div className="qr" role="img" aria-label="QR code that pairs a phone" dangerouslySetInnerHTML={{ __html: qr }} />
            <CodeLine text={link} onCopy={() => copy(link, "Pairing link copied")} />
          </details>
          <div className="pair-short">
            <button type="button" onClick={() => code.mutate()} disabled={code.isPending}>
              {code.data ? "New short code" : "Can't scan? Get a short code"}
            </button>
            {code.data && (
              <p>
                <strong className="pair-short-code mono">{code.data.code}</strong>
                <span className="subtle">
                  On the phone, open <code>{address?.url}/terminals</code> and type this code. It works once, within{" "}
                  {Math.round(code.data.expires_in / 60)} minutes.
                </span>
              </p>
            )}
            {code.isError && <p className="bad">{code.error.message}</p>}
          </div>
          <div className="row wrap">
            <button type="button" className="danger" onClick={() => setConfirmReplace(true)}>
              Replace access key…
            </button>
            <span className="subtle">Signs out every paired device.</span>
          </div>
        </>
      )}
      <ConfirmDialog
        open={confirmReplace}
        title="Replace the access key?"
        confirmLabel={replace.isPending ? "Replacing…" : "Replace key"}
        busy={replace.isPending}
        error={replace.isError ? replace.error.message : null}
        onConfirm={() => replace.mutate()}
        onCancel={() => setConfirmReplace(false)}
      >
        <p>Every paired phone and browser is signed out and needs the new code. Launchers on this computer pick it up by themselves.</p>
      </ConfirmDialog>
    </section>
  );
}
