import { useCallback, useEffect, useRef, useState, type CSSProperties, type FormEvent, type RefObject } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type TerminalSummary } from "../api";
import { Icon } from "../components/Icon";
import { PartTag } from "../components/PartTag";
import { useAuth } from "../hooks";
import { PairPanel, terminalState } from "./Terminals";
import { KEYS, TerminalView, type KeySender } from "./Terminal";

const LAYOUT_KEY = "ai-proxy-terminal-grid-layout";
const MAX_SELECTED = 16;
type Layout = { rows: number; columns: number; fontSize: number; slim: boolean };
const DEFAULT_LAYOUT: Layout = { rows: 2, columns: 2, fontSize: 9, slim: false };

function bounded(value: unknown, min: number, max: number, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value >= min && value <= max ? value : fallback;
}

function readLayout(): Layout {
  try {
    const value = JSON.parse(localStorage.getItem(LAYOUT_KEY) ?? "null");
    return {
      rows: bounded(value?.rows, 1, 4, 2),
      columns: bounded(value?.columns, 1, 4, 2),
      fontSize: bounded(value?.fontSize, 6, 16, 9),
      slim: value?.slim === true,
    };
  } catch { return DEFAULT_LAYOUT; }
}

function readSelection(key: string): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? "[]");
    return Array.isArray(value) ? [...new Set(value.filter((id): id is string => typeof id === "string"))].slice(0, MAX_SELECTED) : [];
  } catch { return []; }
}

const touch = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;

/** Closes a dropdown on Escape or a press outside it, as the account menu does. */
function useDismiss(open: boolean, box: RefObject<HTMLElement | null>, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const handle = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !box.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("mousedown", handle);
    document.addEventListener("keydown", handle);
    return () => {
      document.removeEventListener("mousedown", handle);
      document.removeEventListener("keydown", handle);
    };
  }, [open, box, close]);
}

export function TerminalGridPage() {
  const auth = useAuth();
  // Selections belong to this signed-in person and browser tab, never to a different account.
  const identity = auth.data?.identity;
  const selectionKey = `ai-proxy-terminal-grid:${JSON.stringify([identity?.tenant, identity?.user])}`;
  return <TerminalGrid key={selectionKey} selectionKey={selectionKey} />;
}

function TerminalGrid({ selectionKey }: { selectionKey: string }) {
  const remote = useQuery({ queryKey: ["remote"], queryFn: api.remote, refetchInterval: (query) => query.state.status === "error" ? 10000 : false });
  const trusted = remote.data?.trusted === true;
  const terminals = useQuery({ queryKey: ["terminals"], queryFn: () => api.terminals(), refetchInterval: 3000, enabled: trusted });
  const [layout, setLayout] = useState(readLayout);
  const [selected, setSelected] = useState(() => readSelection(selectionKey));
  const [choosing, setChoosing] = useState(() => readSelection(selectionKey).length === 0);
  const [layoutOpen, setLayoutOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const grid = useRef<HTMLDivElement>(null);
  const pickerBox = useRef<HTMLDivElement>(null);
  const layoutBox = useRef<HTMLDivElement>(null);
  const closePicker = useCallback(() => setChoosing(false), []);
  const closeLayout = useCallback(() => setLayoutOpen(false), []);
  useDismiss(choosing, pickerBox, closePicker);
  useDismiss(layoutOpen, layoutBox, closeLayout);
  // The tile the key bar types into: the last one clicked, tapped or focused.
  const [active, setActive] = useState<string | null>(null);
  const senders = useRef(new Map<string, KeySender>());
  // On a phone the tiles take no typing themselves: a tap on one raises the keyboard in this box.
  const [message, setMessage] = useState("");
  const messageBox = useRef<HTMLTextAreaElement>(null);
  const focusMessage = useCallback(() => messageBox.current?.focus(), []);

  useEffect(() => {
    const element = grid.current;
    if (!element) return;
    // Alerts, the picker and the collapsing bar change the space above the grid without a window resize.
    const fit = () => {
      const top = element.getBoundingClientRect().top + window.scrollY;
      // The key bar sits under the grid, inside the same screen.
      const keys = element.nextElementSibling?.getBoundingClientRect().height ?? 0;
      element.style.setProperty("--grid-height", `${Math.max(0, window.innerHeight - top - keys - 16)}px`);
    };
    const observer = new ResizeObserver(fit);
    observer.observe(element.parentElement!);
    window.addEventListener("resize", fit);
    fit();
    return () => { observer.disconnect(); window.removeEventListener("resize", fit); };
  }, [trusted, selected.length > 0, layout.slim]);

  useEffect(() => {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch { /* Storage may be disabled. */ }
  }, [layout]);
  useEffect(() => {
    try { sessionStorage.setItem(selectionKey, JSON.stringify(selected)); } catch { /* The grid still works without storage. */ }
  }, [selected, selectionKey]);

  const all = terminals.data?.terminals ?? [];
  const byId = new Map(all.map((terminal) => [terminal.session_id, terminal]));
  const capacity = layout.rows * layout.columns;
  const pages = Math.max(1, Math.ceil(selected.length / capacity));
  const currentPage = Math.min(page, pages - 1);
  const visible = selected.slice(currentPage * capacity, (currentPage + 1) * capacity);
  const target = active && visible.includes(active) ? active : visible.find((id) => byId.has(id)) ?? null;
  const targetTerminal = target ? byId.get(target) : undefined;
  const canType = Boolean(targetTerminal?.you?.control) && !(targetTerminal?.ended_at ?? targetTerminal?.session_ended_at);
  const candidates = all.filter((terminal) => selected.includes(terminal.session_id) || !(terminal.ended_at ?? terminal.session_ended_at));
  const matching = candidates.filter((terminal) =>
    [terminal.agent_name, terminal.project, terminal.machine_name, terminal.session_id].some((value) => value?.toLowerCase().includes(search.trim().toLowerCase())),
  );
  const toggle = (id: string) => {
    setSelected((previous) => previous.includes(id) ? previous.filter((item) => item !== id) : previous.length < MAX_SELECTED ? [...previous, id] : previous);
  };
  const changeLayout = (next: Partial<Layout>) => {
    setLayout((previous) => ({ ...previous, ...next }));
    setPage(0);
  };
  // Slimming the bar only trades page chrome for terminal space, so the page stays where it was.
  const toggleSlim = () => {
    const slim = !layout.slim;
    setLayout((previous) => ({ ...previous, slim }));
    // Hiding the controls hides the dropdowns' buttons too: close them with them.
    if (slim) { setChoosing(false); setLayoutOpen(false); }
  };
  // The empty grid's buttons and empty tiles are outside the dropdown: open it on the next click, after its own dismissal.
  const openPicker = () => { setLayoutOpen(false); setChoosing(true); };
  const sendMessage = (event: FormEvent) => {
    event.preventDefault();
    if (target && message.trim() && senders.current.get(target)?.text(message)) setMessage("");
  };

  return (
    <div className="page wide terminal-grid-page">
      <header className={`grid-bar${layout.slim ? " slim" : ""}`}>
        <button type="button" className="icon-button grid-collapse" aria-expanded={!layout.slim} aria-controls="grid-controls"
          title={layout.slim ? "Show the grid controls" : "Hide the grid controls: more room for the terminals"}
          aria-label={layout.slim ? "Show the grid controls" : "Hide the grid controls"} onClick={toggleSlim}>
          <Icon name="chevron" size={15} className={layout.slim ? "" : "down"} />
        </button>
        <PartTag />
        <h1>Live agent view</h1>
        <div className="grid-actions">
          {pages > 1 && <div className="row grid-pages" aria-label="Grid pages">
            <button type="button" className="small" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button>
            <span className="mono" aria-live="polite">{currentPage + 1} / {pages}</span>
            <button type="button" className="small" disabled={currentPage === pages - 1} onClick={() => setPage(currentPage + 1)}>Next</button>
          </div>}
          {trusted && <div className="grid-controls" id="grid-controls" hidden={layout.slim}>
            <div className="grid-menu" ref={pickerBox}>
              <button type="button" className={`small grid-menu-button${choosing ? " open" : ""}`} aria-haspopup="dialog" aria-expanded={choosing} aria-controls="grid-agent-picker"
                onClick={() => { setLayoutOpen(false); setChoosing(!choosing); }}>
                <Icon name="terminal" size={14} /> Agents
                <span className="grid-menu-count mono" aria-label={`${selected.length} selected`}>{selected.length}</span>
                <Icon name="chevron" size={12} className="grid-menu-caret" />
              </button>
              {choosing && <div className="grid-popover grid-picker panel" id="grid-agent-picker" role="dialog" aria-label="Choose agents">
                <input type="search" aria-label="Search agents" placeholder="Search by agent, project or machine" autoFocus={!touch}
                  value={search} onChange={(event) => setSearch(event.target.value)} />
                <div className="grid-agent-options" role="group" aria-label="Agents sharing a terminal">
                  {matching.map((terminal) => {
                    const checked = selected.includes(terminal.session_id);
                    const state = terminalState(terminal);
                    return <label className={`grid-agent-option${checked ? " selected" : ""}`} key={terminal.session_id} title={`${terminal.agent_name} · ${state.label}`}>
                      <input type="checkbox" checked={checked} disabled={!checked && selected.length >= MAX_SELECTED} onChange={() => toggle(terminal.session_id)} />
                      <span className={`state-dot ${state.tone}`} aria-hidden="true" />
                      <span className="grid-agent-name">
                        <strong>{terminal.agent_name}</strong>
                        <span className="subtle">{terminal.project || terminal.session_id} · {terminal.machine_name || "Unknown machine"}{terminal.you?.control ? "" : " · Watch only"}</span>
                      </span>
                      <span className="grid-agent-state subtle">{state.label}</span>
                    </label>;
                  })}
                  {terminals.isPending && <p className="subtle grid-picker-note">Loading agents…</p>}
                  {terminals.isError && <p role="alert" className="bad grid-picker-note">{terminals.error.message}</p>}
                  {terminals.isSuccess && !matching.length && <p className="subtle grid-picker-note">
                    {candidates.length ? "No agents match your search." : "No agents are sharing a terminal. Start one with --control, or from a machine’s page."}{" "}
                    <Link to="/agents">Start an agent</Link>
                  </p>}
                </div>
                <div className="grid-picker-foot">
                  <span className="subtle">{selected.length} of {MAX_SELECTED} selected</span>
                  <button type="button" className="small" disabled={!selected.length} onClick={() => { setSelected([]); setPage(0); }}>Clear</button>
                  <button type="button" className="small primary" onClick={() => setChoosing(false)}>Done</button>
                </div>
              </div>}
            </div>
            <div className="grid-menu" ref={layoutBox}>
              <button type="button" className={`small grid-menu-button${layoutOpen ? " open" : ""}`} aria-haspopup="dialog" aria-expanded={layoutOpen} aria-controls="grid-layout"
                title="Rows, columns and text size" onClick={() => { setChoosing(false); setLayoutOpen(!layoutOpen); }}>
                <Icon name="grid" size={14} /> <span className="mono">{layout.rows} × {layout.columns}</span>
                <Icon name="chevron" size={12} className="grid-menu-caret" />
              </button>
              {layoutOpen && <div className="grid-popover grid-layout-controls panel" id="grid-layout" role="dialog" aria-label="Layout">
                {(["rows", "columns"] as const).map((field) => <label key={field}>
                  {field === "rows" ? "Rows" : "Columns"}
                  <select aria-label={field === "rows" ? "Rows" : "Columns"} value={layout[field]} onChange={(event) => changeLayout({ [field]: Number(event.target.value) })}>
                    {[1, 2, 3, 4].map((value) => <option key={value} value={value}>{value}</option>)}
                  </select>
                </label>)}
                <label>Text size
                  <select aria-label="Text size" value={layout.fontSize} onChange={(event) => changeLayout({ fontSize: Number(event.target.value) })}>
                    {[6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16].map((value) => <option key={value} value={value}>{value}px</option>)}
                  </select>
                </label>
                <p className="subtle">{capacity} per page. {touch ? "Tap a terminal to type to it below." : "Click a terminal to type in it."}</p>
              </div>}
            </div>
          </div>}
        </div>
      </header>

      {remote.isError && <p role="alert" className="bad">{remote.error.message}</p>}
      {remote.isPending && <p className="subtle">Loading agents…</p>}
      {remote.data && !trusted && <PairPanel />}
      {trusted && <>
        {!selected.length ? <div className="empty panel">
          <Icon name="grid" size={32} />
          <h2>Your terminal grid</h2>
          <p className="subtle">Choose the agents you want to watch together. Each terminal keeps its own keyboard focus.</p>
          <button type="button" className="primary" onClick={openPicker}>Choose agents</button>
        </div> : <div ref={grid} className="terminal-grid" style={{ "--grid-rows": layout.rows, "--grid-columns": layout.columns } as CSSProperties}>
          {visible.map((id) => <TerminalTile key={id} id={id} terminal={byId.get(id)} fontSize={layout.fontSize} loading={terminals.isPending}
            active={id === target && visible.length > 1} onActivate={() => setActive(id)} senders={senders.current} onRemove={() => toggle(id)}
            onTapToType={touch ? focusMessage : undefined} />)}
          {Array.from({ length: capacity - visible.length }, (_, index) => <button type="button" className="grid-empty-tile" key={`empty-${index}`} onClick={openPicker}>
            <Icon name="plus" size={24} /><span>Add an agent</span>
          </button>)}
        </div>}
        {selected.length > 0 && <div className="grid-keys">
          {touch && <form className="term-input grid-message" onSubmit={sendMessage}>
            <textarea ref={messageBox} rows={1} value={message} onChange={(event) => setMessage(event.target.value)} disabled={!canType}
              aria-label={`Message for ${targetTerminal?.agent_name ?? "the selected agent"}`}
              placeholder={canType ? `Message for ${targetTerminal?.agent_name ?? "the agent"}` : "Tap a terminal you can type in"} />
            <button type="submit" className="primary" disabled={!canType || !message.trim()}>Send</button>
          </form>}
          <div className="key-bar grid-key-bar" role="toolbar" aria-label={`Keys for ${targetTerminal?.agent_name ?? "the selected agent"}`}>
            <span className="subtle grid-key-target" title="Keys go to the selected terminal: click or tap one to select it">
              <Icon name="terminal" size={14} /> {targetTerminal?.agent_name ?? "No agent"}
            </span>
            {KEYS.map((key) => <button key={key.label} type="button" title={key.title} disabled={!canType}
              onClick={() => { if (target) senders.current.get(target)?.keys(key.keys); }}>{key.label}</button>)}
          </div>
        </div>}
      </>}
    </div>
  );
}

function TerminalTile({ id, terminal, fontSize, loading, active, onActivate, senders, onRemove, onTapToType }: {
  id: string; terminal?: TerminalSummary; fontSize: number; loading: boolean; active: boolean;
  onActivate: () => void; senders: Map<string, KeySender>; onRemove: () => void; onTapToType?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const onSender = useCallback((sender: KeySender | null) => {
    if (sender) senders.set(id, sender);
    else senders.delete(id);
  }, [id, senders]);
  const state = terminalState(terminal);
  const name = terminal?.agent_name ?? "Unavailable session";
  const metaId = `tile-meta-${id}`;
  return <section className={`terminal-tile tone-${state.tone}${active ? " active" : ""}`} aria-label={`${name} terminal`}
    onPointerDown={onActivate} onFocus={onActivate}>
    <header className="terminal-tile-head">
      <button type="button" className="terminal-tile-title" aria-expanded={open} aria-controls={metaId} title={`${name} · ${state.label}`}
        onClick={() => setOpen((previous) => !previous)}>
        <Icon name="chevron" size={12} className={`tile-chevron${open ? " open" : ""}`} />
        <span className={`state-dot ${state.tone}`} aria-hidden="true" />
        <span className="terminal-tile-name">{name}</span>
      </button>
      <Link className="button icon-button" to={`/terminals/${encodeURIComponent(id)}`} state={{ fromGrid: true }} title="Fullscreen: open the full terminal view" aria-label={`Fullscreen ${name}`}><Icon name="expand" size={16} /></Link>
      <button type="button" className="icon-button" title="Remove from grid" aria-label={`Remove ${name} from grid`} onClick={onRemove}><Icon name="close" size={14} /></button>
    </header>
    {open && <p className="terminal-tile-meta" id={metaId}>
      {terminal?.agent && <span><b>Type</b> <span className="mono">{terminal.agent}</span></span>}
      <span><b>Machine</b> {terminal?.machine_name ?? "Unknown machine"}</span>
      {terminal?.project && <span title={terminal.project_path ?? undefined}><b>Folder</b> {terminal.project}</span>}
      <span><b>Status</b> {state.label}{state.detail ? ` — ${state.detail}` : ""}</span>
      {terminal?.model && <span className="meta-model"><b>Model</b> {terminal.model}</span>}
      <span className="meta-session"><b>Session</b> <span className="mono">{id}</span></span>
      {terminal && !terminal.you?.control && <span>Watch only: you cannot type in this terminal.</span>}
    </p>}
    {terminal ? <TerminalView sessionId={id} compact gridFontSize={fontSize} onSender={onSender} onTapToType={onTapToType} /> : <p className="grid-unavailable subtle">{loading ? "Loading terminal…" : "This session is no longer available. Remove it from the grid to choose another."}</p>}
  </section>;
}
