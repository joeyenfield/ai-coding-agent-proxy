import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { TerminalSummary } from "../api";
import { useToast } from "../hooks";

/** Whether this browser shows a system notification when an agent starts waiting, kept per browser. */
const NOTIFY_KEY = "ai-proxy-notify";

const terminalPath = (id: string) => `/terminals/${encodeURIComponent(id)}`;
const nameOf = (terminal: TerminalSummary) => `${terminal.agent_name}${terminal.project ? ` · ${terminal.project}` : ""}`;

/** Agents that wait for their user now, asking ones first. */
export function waitingAgents(terminals: TerminalSummary[] | undefined): TerminalSummary[] {
  return (terminals ?? [])
    .filter((item) => item.attention && !item.ended_at && !item.session_ended_at)
    .sort((a, b) => Number(b.attention?.kind === "question") - Number(a.attention?.kind === "question"));
}

export function notificationsSupported() {
  return typeof window !== "undefined" && "Notification" in window;
}

export function useNotifyChoice(): [boolean, (on: boolean) => Promise<void>] {
  const [on, setOn] = useState(() => notificationsSupported() && Notification.permission === "granted" && localStorage.getItem(NOTIFY_KEY) === "1");
  const choose = async (value: boolean) => {
    if (value && notificationsSupported() && Notification.permission !== "granted") {
      if ((await Notification.requestPermission()) !== "granted") value = false;
    }
    localStorage.setItem(NOTIFY_KEY, value ? "1" : "0");
    setOn(value);
    window.dispatchEvent(new Event(NOTIFY_KEY));
  };
  useEffect(() => {
    const changed = () => setOn(notificationsSupported() && Notification.permission === "granted" && localStorage.getItem(NOTIFY_KEY) === "1");
    window.addEventListener(NOTIFY_KEY, changed);
    return () => window.removeEventListener(NOTIFY_KEY, changed);
  }, []);
  return [on, choose];
}

/**
 * Tells the user when an agent starts waiting for them: a toast and, if they turned it on, a
 * system notification; the page title counts waiting agents. Agents asking something (a
 * permission prompt) also get a banner above every page until they're answered or dismissed.
 */
export function AttentionAlerts({ terminals }: { terminals: TerminalSummary[] | undefined }) {
  const notify = useToast();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [notifyOn] = useNotifyChoice();
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  // What each agent was doing at the last poll; undefined before the first, so agents already waiting when the page opens don't all alert.
  const seen = useRef<Map<string, string> | undefined>(undefined);
  const waiting = waitingAgents(terminals);
  const key = (terminal: TerminalSummary) => `${terminal.session_id}@${terminal.attention?.since ?? ""}`;

  useEffect(() => {
    if (!terminals) return;
    const previous = seen.current;
    const now = new Map(waiting.map((item) => [item.session_id, item.attention!.kind]));
    seen.current = now;
    if (!previous) return;
    for (const terminal of waiting) {
      const before = previous.get(terminal.session_id);
      const kind = terminal.attention!.kind;
      // A new wait, or a quiet agent that has since put a question up.
      if (before === kind || (before === "question" && kind === "idle")) continue;
      if (pathname === terminalPath(terminal.session_id) && document.visibilityState === "visible") continue;
      const body = kind === "question" ? terminal.attention!.question ?? "It's asking you something." : "It's done for now and waiting for you.";
      notify(`${nameOf(terminal)}: ${kind === "question" ? "needs you" : "waiting"}`);
      if (notifyOn && notificationsSupported()) {
        const shown = new Notification(`${nameOf(terminal)} ${kind === "question" ? "needs you" : "is waiting"}`, { body, tag: terminal.session_id });
        shown.onclick = () => {
          window.focus();
          navigate(terminalPath(terminal.session_id));
          shown.close();
        };
      }
    }
  }, [terminals]);

  const count = waiting.length;
  useEffect(() => {
    const base = document.title.replace(/^\(\d+\) /, "");
    document.title = count ? `(${count}) ${base}` : base;
  }, [count]);

  const asking = waiting.filter((item) => item.attention?.kind === "question" && pathname !== terminalPath(item.session_id) && !dismissed.has(key(item)));
  if (!asking.length) return null;
  return (
    <div className="attention" role="status" aria-live="polite">
      {asking.slice(0, 3).map((terminal) => (
        <p key={terminal.session_id} className="attention-item">
          <span className="attention-text">
            <strong>{nameOf(terminal)}</strong>
            {terminal.machine_name && <span className="subtle"> on {terminal.machine_name}</span>}
            <span className="attention-question">{terminal.attention?.question ?? "It's asking you something."}</span>
          </span>
          <Link to={terminalPath(terminal.session_id)} className="button primary small">
            Answer
          </Link>
          <button type="button" className="small" onClick={() => setDismissed(new Set(dismissed).add(key(terminal)))}>
            Later
          </button>
        </p>
      ))}
      {asking.length > 3 && (
        <p className="subtle">
          And {asking.length - 3} more on <Link to="/terminals">Running agents</Link>.
        </p>
      )}
    </div>
  );
}

/** Turns system notifications on and off for this browser, where it has them. */
export function NotifyToggle() {
  const [on, choose] = useNotifyChoice();
  if (!notificationsSupported()) return null;
  const blocked = Notification.permission === "denied";
  return (
    <label className="check" title={blocked ? "This browser blocks notifications from this site; allow them in its site settings." : undefined}>
      <input type="checkbox" checked={on} disabled={blocked} onChange={(event) => void choose(event.target.checked)} />
      Notify me when an agent is waiting{blocked && <span className="subtle"> (blocked in this browser)</span>}
    </label>
  );
}
