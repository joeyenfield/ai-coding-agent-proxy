import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { API_ORIGIN, api, type Status } from "../api";
import { ConfirmDialog } from "./ConfirmDialog";

interface Restarting {
  before: string;
  waiting: { requests: number };
  /** Where the proxy listens after the restart, when that's not where this page came from. */
  moved: string | null;
  startedAt: number;
}

const RestartContext = createContext<(status: Status) => Promise<void>>(async () => undefined);

/** Where the restarted proxy will answer, if that's not the port this page came from. */
function newAddress(port: number): string | null {
  if (API_ORIGIN) return null;
  if (String(port) === (window.location.port || (window.location.protocol === "https:" ? "443" : "80"))) return null;
  // The Vite dev server proxies to the proxy; it keeps its own port.
  if (window.location.port === "5173") return null;
  return `${window.location.protocol}//${window.location.hostname}:${port}${window.location.pathname}`;
}

/** Restarting the proxy from the console: asks it to restart, then waits for the new instance and reloads. */
export function RestartProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Restarting | null>(null);
  const [gone, setGone] = useState(false);
  const restart = useCallback(async (status: Status) => {
    const result = await api.restartServer();
    setState({ before: status.proxy_instance_id, waiting: result.waiting_for, moved: newAddress(result.listen.port), startedAt: Date.now() });
  }, []);

  useEffect(() => {
    if (!state) return;
    let cancelled = false;
    const poll = async () => {
      while (!cancelled) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        try {
          const body = await api.health();
          if (body.proxy_instance_id && body.proxy_instance_id !== state.before) {
            window.location.reload();
            return;
          }
        } catch {
          setGone(true);
          // It listens elsewhere now: follow it once it has had a moment to start.
          if (state.moved && Date.now() - state.startedAt > 4000) {
            window.location.href = state.moved;
            return;
          }
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
    };
  }, [state]);

  return (
    <RestartContext.Provider value={restart}>
      {children}
      {state && (
        <div className="restart-overlay" role="status" aria-live="polite">
          <div className="panel">
            <h2>Restarting the proxy…</h2>
            {!gone && state.waiting.requests > 0 ? (
              <p className="subtle">
                Waiting for {state.waiting.requests} {state.waiting.requests === 1 ? "request" : "requests"} to finish (at most 30
                seconds).
              </p>
            ) : (
              <p className="subtle">Reloading its configuration. This page reloads when it's back.</p>
            )}
            {state.moved && <p className="subtle">It now listens at {state.moved}; this page follows it there.</p>}
          </div>
        </div>
      )}
    </RestartContext.Provider>
  );
}

/** A button that restarts the proxy after asking, for Settings and the restart banner. */
export function RestartButton({ status, label = "Restart the proxy…", className = "" }: { status: Status; label?: string; className?: string }) {
  const restart = useContext(RestartContext);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!status.restart?.available) return null;
  return (
    <>
      <button type="button" className={className} disabled={status.restart.pending} onClick={() => setConfirm(true)}>
        {status.restart.pending ? "Restarting…" : label}
      </button>
      <ConfirmDialog
        open={confirm}
        title="Restart the proxy?"
        confirmLabel={busy ? "Restarting…" : "Restart"}
        busy={busy}
        error={error}
        onCancel={() => setConfirm(false)}
        onConfirm={async () => {
          setBusy(true);
          setError(null);
          try {
            await restart(status);
            setConfirm(false);
          } catch (failure) {
            setError(failure instanceof Error ? failure.message : String(failure));
          } finally {
            setBusy(false);
          }
        }}
      >
        <p>
          It finishes the requests in flight (up to 30 seconds), then starts again with the configuration in{" "}
          <code>config/</code>: backends, model settings, agents, the listen address and HTTPS. It also rebuilds the console if
          its sources changed.
        </p>
        <p className="subtle">
          Running agents keep going: their terminals and the live views reconnect by themselves.
          {status.restart.address_pinned ? " The listen address stays, because --host or --port set it." : ""}
        </p>
      </ConfirmDialog>
    </>
  );
}
