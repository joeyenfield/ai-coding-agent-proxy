import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Terminal, type ITheme } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { api, onRenewed, renewSignIn, terminalSocketUrl, type TerminalInfo } from "../api";
import { SharingControl } from "../components/Sharing";
import { useClipboard, useToast } from "../hooks";
import { PairPanel, terminalState } from "./Terminals";

type Connection = "connecting" | "open" | "retrying" | "waiting" | "unauthorized" | "missing";

/** How long a terminal that isn't there yet is waited for: a launch from the console opens its page as soon as the
 * session exists, a moment before the agent's launcher has connected its terminal. */
const WAIT_FOR_TERMINAL_MS = 60_000;

const THEMES: Record<"dark" | "light", ITheme> = {
  dark: { background: "#11141a", foreground: "#e6eaf0", cursor: "#e6eaf0", cursorAccent: "#11141a", selectionBackground: "#3d4552" },
  light: { background: "#ffffff", foreground: "#1b2330", cursor: "#1b2330", cursorAccent: "#ffffff", selectionBackground: "#c4ccd7" },
};
const FONT = '"JetBrains Mono", ui-monospace, "Cascadia Mono", Consolas, monospace';
const SIZES = { min: 6, max: 22 };

/**
 * What xterm.js sends back when the agent queries its terminal: cursor position, device attributes,
 * focus changes, colours, mouse reports. The real terminal on the computer already answers those,
 * so they must never be typed into the agent.
 */
const REPLY = /^(?:\x1b\[[?>]?[\d;]*[cnRIO]|\x1b\[\?[\d;]*u|\x1b\][\s\S]*(?:\x07|\x1b\\)|\x1b\[<[\d;]*[Mm]|\x1b\[M[\s\S]{3}|\x1b\[\?[\d;]*\$y|\x1bP[\s\S]*\x1b\\)$/;

/**
 * Mouse-wheel reports (SGR buttons 64-67, or the older X10 form), let through though they look like
 * replies: full-screen agents (Claude Code, OpenCode, Qwen Code) keep their own history in the
 * alternate screen, which has no terminal scrollback, and scroll it on these. Clicks stay filtered,
 * so tapping the terminal on a phone doesn't click inside the agent.
 */
const WHEEL = /^(?:\x1b\[<6[4-7];\d+;\d+M|\x1b\[M[\x60-\x63][\s\S]{2})$/;

/** Arrow keys as the agent expects them: application cursor mode changes the sequence. */
const arrow = (term: Terminal | null, key: string) => (term?.modes.applicationCursorKeysMode ? `\x1bO${key}` : `\x1b[${key}`);

/**
 * Scrolling the agent's own history, for phones, where touch makes no wheel events: wheel reports
 * (a few notches, at the middle of its screen) when it listens to the mouse, else Page Up or Down.
 */
const scroll = (term: Terminal | null, up: boolean) => {
  if (term && term.modes.mouseTrackingMode !== "none") {
    const report = `\x1b[<${up ? 64 : 65};${Math.ceil(term.cols / 2)};${Math.ceil(term.rows / 2)}M`;
    return report.repeat(5);
  }
  return up ? "\x1b[5~" : "\x1b[6~";
};

const KEYS: { label: string; title: string; keys: (term: Terminal | null) => string }[] = [
  { label: "Esc", title: "Escape: interrupt the agent or close a menu", keys: () => "\x1b" },
  { label: "Enter", title: "Enter", keys: () => "\r" },
  { label: "↑", title: "Up", keys: (term) => arrow(term, "A") },
  { label: "↓", title: "Down", keys: (term) => arrow(term, "B") },
  { label: "←", title: "Left", keys: (term) => arrow(term, "D") },
  { label: "→", title: "Right", keys: (term) => arrow(term, "C") },
  { label: "Scroll ↑", title: "Scroll up through the agent's history (like the mouse wheel)", keys: (term) => scroll(term, true) },
  { label: "Scroll ↓", title: "Scroll down through the agent's history", keys: (term) => scroll(term, false) },
  { label: "PgUp", title: "Page Up", keys: () => "\x1b[5~" },
  { label: "PgDn", title: "Page Down", keys: () => "\x1b[6~" },
  { label: "Tab", title: "Tab", keys: () => "\t" },
  { label: "⇧Tab", title: "Shift+Tab: in Claude Code, switches between modes such as plan and auto-accept", keys: () => "\x1b[Z" },
  { label: "Ctrl+C", title: "Ctrl+C: cancel; pressed twice, most agents quit", keys: () => "\x03" },
  { label: "1", title: "1: pick the first option", keys: () => "1" },
  { label: "2", title: "2: pick the second option", keys: () => "2" },
  { label: "3", title: "3: pick the third option", keys: () => "3" },
  { label: "y", title: "y: yes", keys: () => "y" },
  { label: "n", title: "n: no", keys: () => "n" },
];

/**
 * The terminal's text: what's selected, else the screen as it is, or (all) the whole buffer with
 * its scrollback. A full-screen agent (the alternate screen) has no scrollback: all is its screen.
 */
function terminalText(term: Terminal, all: boolean): string {
  if (!all && term.hasSelection()) return term.getSelection();
  const buffer = term.buffer.active;
  const first = all ? 0 : buffer.viewportY;
  const last = all ? buffer.length : Math.min(buffer.length, buffer.viewportY + term.rows);
  const lines: string[] = [];
  for (let row = first; row < last; row++) {
    const line = buffer.getLine(row);
    if (!line) continue;
    const text = line.translateToString(true);
    // A wrapped line carries on the one before it.
    if (line.isWrapped && lines.length) lines[lines.length - 1] += text;
    else lines.push(text);
  }
  return lines.join("\n").replace(/\s+$/, "");
}

function useStored<T extends string | number>(key: string, initial: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    const stored = localStorage.getItem(key);
    if (stored == null) return initial;
    return (typeof initial === "number" ? Number(stored) || initial : stored) as T;
  });
  const update = useCallback(
    (next: T) => {
      localStorage.setItem(key, String(next));
      setValue(next);
    },
    [key],
  );
  return [value, update];
}

/** Width of one monospace cell per pixel of font size, measured once. */
let cellRatio = 0;
function cellWidth(fontSize: number) {
  if (!cellRatio) {
    const context = document.createElement("canvas").getContext("2d");
    if (context) {
      context.font = `100px ${FONT}`;
      cellRatio = context.measureText("W").width / 100;
    }
    cellRatio ||= 0.6;
  }
  return fontSize * cellRatio;
}

export function TerminalPage() {
  const { sessionId = "" } = useParams();
  const notify = useToast();
  const copy = useClipboard();
  // The terminal's key handler is made once; it copies through the latest hook.
  const copyRef = useRef(copy);
  copyRef.current = copy;
  const list = useQuery({ queryKey: ["terminals"], queryFn: () => api.terminals(), refetchInterval: 3000, retry: false });
  const summary = list.data?.terminals.find((item) => item.session_id === sessionId);
  const [info, setInfo] = useState<TerminalInfo | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [fontSize, setFontSize] = useStored<number>("ai-proxy-terminal-font", 13);
  const [background, setBackground] = useStored<"dark" | "light">("ai-proxy-terminal-background", "dark");
  const [text, setText] = useState("");
  const screen = useRef<HTMLDivElement>(null);
  const term = useRef<Terminal | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const touch = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
  // Shared with you to watch only: no typing (the proxy refuses it too).
  const watchOnly = Boolean(summary?.you && !summary.you.control);
  const watchOnlyRef = useRef(watchOnly);
  watchOnlyRef.current = watchOnly;

  const send = useCallback(
    (message: { keys?: string; text?: string; enter?: boolean }) => {
      if (socket.current?.readyState !== WebSocket.OPEN) {
        notify("Not connected to the terminal yet.");
        return false;
      }
      socket.current.send(JSON.stringify({ type: "input", ...message }));
      return true;
    },
    [notify],
  );

  useEffect(() => {
    const terminal = new Terminal({
      cols: 80, rows: 24, scrollback: 20000, fontFamily: FONT, fontSize, theme: THEMES[background],
      cursorBlink: false, disableStdin: touch, allowProposedApi: false,
      // When the agent takes the mouse, Shift+drag selects anyway; on a Mac, Option+drag does too.
      macOptionClickForcesSelection: true,
    });
    terminal.open(screen.current!);
    term.current = terminal;
    // Copying a selection: Ctrl+Shift+C or Cmd+C, and Ctrl+C while something is selected (with
    // nothing selected, Ctrl+C still goes to the agent).
    terminal.attachCustomKeyEventHandler((event) => {
      if (event.type !== "keydown" || event.key.toLowerCase() !== "c" || event.altKey) return true;
      const shortcut = (event.ctrlKey && event.shiftKey) || (event.metaKey && !event.ctrlKey);
      if (!shortcut && !(event.ctrlKey && terminal.hasSelection())) return true;
      event.preventDefault();
      if (terminal.hasSelection()) {
        void copyRef.current(terminal.getSelection(), "Copied the selection");
        terminal.clearSelection();
      }
      return false;
    });
    let stopped = false;
    let retry: number | undefined;
    // The byte offset of the end of the output shown, when the server has said (null: unknown).
    let position: number | null = null;
    // Whether a 4401 has already been answered by renewing the sign-in, since the last good connection.
    let renewed = false;
    // A reconnect after the server closed for renewal shows "retrying" only if it takes longer than this.
    let slow: number | undefined;
    const waitUntil = Date.now() + WAIT_FOR_TERMINAL_MS;
    const typed = terminal.onData((data) => {
      if ((WHEEL.test(data) || !REPLY.test(data)) && !watchOnlyRef.current) send({ keys: data });
    });
    const connect = async () => {
      try {
        const status = await api.authStatus();
        if (stopped) return;
        if (!status.identity) {
          setConnection("unauthorized");
          return;
        }
      } catch (error) {
        if (stopped) return;
        notify(error instanceof Error ? error.message : "Sign-in is unavailable");
        setConnection("retrying");
        retry = window.setTimeout(connect, 5000);
        return;
      }
      const opened = Date.now();
      const ws = new WebSocket(terminalSocketUrl(sessionId, position));
      ws.binaryType = "arraybuffer";
      socket.current = ws;
      // After its state, the server says where its replay starts ("replay") and whether that carries
      // on from `from`. Until then nothing is reset: an older server sends no "replay" and starts the
      // history from the beginning, so the first history frame (or "replayed") resets instead.
      let replaying = true;
      let awaitingReplay = false;
      const startOver = () => {
        awaitingReplay = false;
        position = null;
        terminal.reset();
      };
      ws.onmessage = (event) => {
        if (typeof event.data !== "string") {
          if (awaitingReplay) startOver();
          const bytes = new Uint8Array(event.data as ArrayBuffer);
          if (position != null) position += bytes.byteLength;
          terminal.write(bytes);
          return;
        }
        const message = JSON.parse(event.data);
        if (message.type === "state") {
          if (replaying) awaitingReplay = true;
          const next = message.terminal as TerminalInfo;
          if (terminal.cols !== next.cols || terminal.rows !== next.rows) terminal.resize(next.cols, next.rows);
          setInfo(next);
          window.clearTimeout(slow);
          slow = undefined;
          setConnection("open");
          renewed = false;
        } else if (message.type === "replay") {
          awaitingReplay = false;
          if (!message.resumed) terminal.reset();
          position = typeof message.start === "number" ? message.start : null;
        } else if (message.type === "replayed") {
          if (awaitingReplay) startOver();
          replaying = false;
          terminal.scrollToBottom();
        } else if (message.type === "gap") {
          // The launcher skipped some output; what follows starts here.
          if (typeof message.to === "number") position = message.to;
        } else if (message.type === "resize") {
          terminal.resize(message.cols, message.rows);
        } else if (message.type === "error") {
          notify(message.message);
        }
      };
      ws.onclose = (event) => {
        if (stopped) return;
        if (event.code === 4401 && !renewed) {
          // The access token ran out between the status check and the dial: renew it once and try again.
          renewed = true;
          setConnection("retrying");
          void renewSignIn(opened).then(() => {
            if (!stopped) connect();
          });
        } else if (event.code === 4401) setConnection("unauthorized");
        else if (event.code === 4410) {
          // The access token this socket opened with ran out (they last minutes) or couldn't be checked.
          // The console renews its cookie ahead of time, so dial again at once, carrying on from what's shown.
          window.clearTimeout(slow);
          slow = window.setTimeout(() => {
            if (!stopped) setConnection("retrying");
          }, 1500);
          retry = window.setTimeout(connect, 0);
        }
        else if (event.code === 4404 && Date.now() < waitUntil) {
          setConnection("waiting");
          retry = window.setTimeout(connect, 1500);
        } else if (event.code === 4404) setConnection("missing");
        else {
          // The proxy restarted or a network blip: reconnect, carrying on from what's already shown.
          setConnection("retrying");
          retry = window.setTimeout(connect, 2000);
        }
      };
    };
    connect();
    // Each renewal of the console's access token goes over the open socket too, so the server keeps it
    // open past the token it was dialled with (otherwise it closes with 4410, and the code above redials).
    const unrenewed = onRenewed((token) => {
      const ws = socket.current;
      if (token && ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "token", token }));
    });
    return () => {
      stopped = true;
      unrenewed();
      window.clearTimeout(retry);
      window.clearTimeout(slow);
      socket.current?.close();
      typed.dispose();
      terminal.dispose();
      term.current = null;
    };
    // Font and colours are applied below without reconnecting.
  }, [sessionId, send, touch]);

  useEffect(() => {
    if (term.current) term.current.options.fontSize = fontSize;
  }, [fontSize]);
  useEffect(() => {
    if (term.current) term.current.options.theme = THEMES[background];
  }, [background]);

  const cols = info?.cols ?? summary?.cols ?? 80;
  const fit = () => {
    const width = (screen.current?.clientWidth ?? 0) - 16;
    if (width > 0) setFontSize(Math.max(SIZES.min, Math.min(SIZES.max, Math.floor((width / (cols * cellWidth(1))) * 2) / 2)));
  };
  const resize = (step: number) => setFontSize(Math.max(SIZES.min, Math.min(SIZES.max, fontSize + step)));

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!text.trim()) return;
    if (send({ text, enter: true })) setText("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // On a keyboard, Enter sends and Shift+Enter starts a new line; on a phone, the Send button sends.
    if (!touch && event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  const merged = summary ? { ...summary, ...(info ?? {}) } : undefined;
  const state = terminalState(merged);
  const live = Boolean(info?.connected) && !watchOnly;
  const name = summary?.agent_name ?? info?.command ?? "Agent";

  return (
    <div className="page wide terminal-page">
      <header className="page-head">
        <p className="breadcrumbs">
          <Link to="/terminals">Running agents</Link>
          <span aria-hidden="true"> › </span>
          <span>{name}</span>
        </p>
        <div className="section-head">
          <h1>
            {name}
            {summary?.project && <span className="subtle"> · {summary.project}</span>}
          </h1>
          <span className={`pill ${state.tone === "idle" ? "" : state.tone}`}>{state.label}</span>
        </div>
        <p className="subtle">
          {state.detail}
          {state.detail && " · "}
          <Link to={`/sessions/${encodeURIComponent(sessionId)}`}>Session traffic</Link>
        </p>
        <SharingControl sessionId={sessionId} you={summary?.you} />
      </header>

      {connection === "unauthorized" && <PairPanel />}
      {connection === "missing" && (
        <div className="empty panel">
          <p>This session has no shared terminal.</p>
          <p className="subtle">
            Only agents started with <code>agent … --control</code> share their terminal.
          </p>
        </div>
      )}

      <section className="terminal-view" hidden={connection === "unauthorized" || connection === "missing"} aria-label="Agent terminal">
        <div className="terminal-toolbar">
          <div className="row" role="group" aria-label="Text size">
            <button type="button" onClick={() => resize(-1)} aria-label="Smaller text" disabled={fontSize <= SIZES.min}>
              A−
            </button>
            <span className="mono subtle">{fontSize}px</span>
            <button type="button" onClick={() => resize(1)} aria-label="Larger text" disabled={fontSize >= SIZES.max}>
              A+
            </button>
            <button type="button" onClick={fit} title="Size the text so the whole terminal width fits">
              Fit width
            </button>
          </div>
          <div className="row">
            <label className="check">
              <input type="checkbox" checked={background === "light"} onChange={(event) => setBackground(event.target.checked ? "light" : "dark")} />
              Light background
            </label>
            <button type="button" onClick={() => term.current?.scrollToBottom()}>
              Latest
            </button>
            <button
              type="button"
              title="Copy what's selected, or else what's on the screen"
              onClick={() => {
                const current = term.current;
                if (!current) return;
                const selected = current.hasSelection();
                void copy(terminalText(current, false), selected ? "Copied the selection" : "Copied the screen");
              }}
            >
              Copy
            </button>
            <button type="button" title="Copy everything in the terminal, its history included" onClick={() => term.current && void copy(terminalText(term.current, true), "Copied the whole terminal")}>
              Copy all
            </button>
          </div>
        </div>
        {connection === "retrying" && <p className="banner warn">Lost the connection to the proxy. Reconnecting…</p>}
        {connection === "waiting" && <p className="banner">Waiting for the agent to share its terminal…</p>}
        {info && !live && (
          <p className="banner">
            {info.ended_at
              ? "The agent has exited. This is the terminal as it was left."
              : "The launcher isn't connected right now, so typing won't reach the agent."}
          </p>
        )}
        <div className="term-screen" ref={screen} style={{ background: THEMES[background].background }} />
        {!touch && (
          <p className="subtle term-hint">
            {live && "Click the terminal to type into it directly, or use the box below. "}
            Select text by dragging, or with Shift+drag when the agent uses the mouse (Option+drag on a Mac); Ctrl+C copies a selection.
          </p>
        )}

        <div className="term-controls">
          <form className="term-input" onSubmit={submit}>
            <textarea
              value={text}
              rows={2}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={watchOnly ? "Shared with you to watch, not to type into" : !live ? "The agent isn't connected" : summary?.agent === "shell" ? "A command to run" : "Message for the agent"}
              aria-label="Message for the agent"
              disabled={!live}
            />
            <button type="submit" className="primary" disabled={!live || !text.trim()}>
              Send
            </button>
          </form>
          <div className="key-bar" role="toolbar" aria-label="Keys">
            {KEYS.map((key) => (
              <button key={key.label} type="button" title={key.title} disabled={!live} onClick={() => send({ keys: key.keys(term.current) })}>
                {key.label}
              </button>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
