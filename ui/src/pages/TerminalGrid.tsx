import { useEffect, useRef, useState, type CSSProperties } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type TerminalSummary } from "../api";
import { Icon } from "../components/Icon";
import { PartTag } from "../components/PartTag";
import { useAuth } from "../hooks";
import { PairPanel, terminalState } from "./Terminals";
import { TerminalView } from "./Terminal";

const LAYOUT_KEY = "ai-proxy-terminal-grid-layout";
const MAX_SELECTED = 16;
type Layout = { rows: number; columns: number; fontSize: number };
const DEFAULT_LAYOUT: Layout = { rows: 2, columns: 2, fontSize: 9 };

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
    };
  } catch { return DEFAULT_LAYOUT; }
}

function readSelection(key: string): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? "[]");
    return Array.isArray(value) ? [...new Set(value.filter((id): id is string => typeof id === "string"))].slice(0, MAX_SELECTED) : [];
  } catch { return []; }
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
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const grid = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = grid.current;
    if (!element) return;
    // Alerts and the picker can change the space above the grid without a window resize.
    const fit = () => {
      const top = element.getBoundingClientRect().top + window.scrollY;
      element.style.setProperty("--grid-height", `${Math.max(0, window.innerHeight - top - 24)}px`);
    };
    const observer = new ResizeObserver(fit);
    observer.observe(element.parentElement!);
    window.addEventListener("resize", fit);
    fit();
    return () => { observer.disconnect(); window.removeEventListener("resize", fit); };
  }, [trusted, selected.length > 0]);

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

  return (
    <div className="page wide terminal-grid-page">
      <header className="page-head">
        <PartTag />
        <h1>Running agents <span className="pill">Beta</span></h1>
        <p className="lede">Keep your sessions in view. Choose agents, click a terminal to type, or open its full view.</p>
      </header>

      {remote.isError && <p role="alert" className="bad">{remote.error.message}</p>}
      {remote.isPending && <p className="subtle">Loading agents…</p>}
      {remote.data && !trusted && <PairPanel />}
      {trusted && <>
        <div className="grid-toolbar panel">
          <button type="button" className={choosing ? "primary" : ""} aria-expanded={choosing} aria-controls="grid-agent-picker" onClick={() => setChoosing(!choosing)}>
            <Icon name="grid" /> Choose agents <span className="mono">{selected.length}</span>
          </button>
          <div className="grid-layout-controls">
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
          </div>
          <span className="subtle grid-capacity">{layout.rows} × {layout.columns} · {capacity} {capacity === 1 ? "terminal" : "terminals"} per page</span>
        </div>

        {terminals.isError && <p role="alert" className="bad">{terminals.error.message}</p>}
        {choosing && <section className="panel grid-picker" id="grid-agent-picker" aria-label="Choose agents">
          <div className="section-head">
            <h2>Choose your agents</h2>
            <div className="row">
              <button type="button" disabled={!selected.length} onClick={() => { setSelected([]); setPage(0); }}>Clear selection</button>
              <button type="button" onClick={() => setChoosing(false)}>Done</button>
            </div>
          </div>
          <input type="search" aria-label="Search agents" placeholder="Search by agent, project or machine" value={search} onChange={(event) => setSearch(event.target.value)} />
          <p className="subtle">Select up to {MAX_SELECTED} sessions. Extra sessions appear on the next grid page.</p>
          <div className="grid-agent-options">
            {matching.map((terminal) => {
              const checked = selected.includes(terminal.session_id);
              const state = terminalState(terminal);
              return <label className={`grid-agent-option ${checked ? "selected" : ""}`} key={terminal.session_id}>
                <input type="checkbox" checked={checked} disabled={!checked && selected.length >= MAX_SELECTED} onChange={() => toggle(terminal.session_id)} />
                <span className="grid-agent-name"><strong>{terminal.agent_name}</strong><span className="subtle">{terminal.project || terminal.session_id}</span><span className="subtle">{terminal.machine_name || "Unknown machine"}{terminal.you?.control ? "" : " · Watch only"}</span></span>
                <span className={`pill ${state.tone === "idle" ? "" : state.tone}`}>{state.label}</span>
              </label>;
            })}
          </div>
          {terminals.isPending && <p className="subtle">Loading agents…</p>}
          {terminals.isSuccess && !matching.length && <p className="subtle">{candidates.length ? "No agents match your search." : "No agents are sharing a terminal. Start one with --control, or from a machine’s page."} <Link to="/agents">Start an agent</Link></p>}
        </section>}

        {!selected.length ? <div className="empty panel">
          <Icon name="grid" size={32} />
          <h2>Your terminal grid</h2>
          <p className="subtle">Choose the agents you want to watch together. Each terminal keeps its own keyboard focus.</p>
          <button type="button" onClick={() => setChoosing(true)}>Choose agents</button>
        </div> : <>
          <div className="section-head grid-status">
            <p className="subtle">{selected.length} selected · Click a terminal to interact. Fullscreen opens the full terminal controls.</p>
            {pages > 1 && <div className="row" aria-label="Grid pages">
              <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button>
              <span className="mono" aria-live="polite">{currentPage + 1} / {pages}</span>
              <button type="button" disabled={currentPage === pages - 1} onClick={() => setPage(currentPage + 1)}>Next</button>
            </div>}
          </div>
          <div ref={grid} className="terminal-grid" style={{ "--grid-rows": layout.rows, "--grid-columns": layout.columns } as CSSProperties}>
            {visible.map((id) => <TerminalTile key={id} id={id} terminal={byId.get(id)} fontSize={layout.fontSize} loading={terminals.isPending} onRemove={() => toggle(id)} />)}
            {Array.from({ length: capacity - visible.length }, (_, index) => <button type="button" className="grid-empty-tile" key={`empty-${index}`} onClick={() => setChoosing(true)}>
              <Icon name="plus" size={24} /><span>Add an agent</span>
            </button>)}
          </div>
        </>}
      </>}
    </div>
  );
}

function TerminalTile({ id, terminal, fontSize, loading, onRemove }: {
  id: string; terminal?: TerminalSummary; fontSize: number; loading: boolean; onRemove: () => void;
}) {
  const state = terminalState(terminal);
  const name = terminal?.agent_name ?? "Unavailable session";
  return <section className={`terminal-tile tone-${state.tone}`} aria-label={`${name} terminal`}>
    <header className="terminal-tile-head">
      <div className="grid-agent-name">
        <strong>{name}</strong>
        <span className="subtle" title={terminal?.project ?? undefined}>{[terminal?.machine_name, terminal?.project].filter(Boolean).join(" · ") || id}</span>
      </div>
      {terminal && <span className={`pill ${state.tone === "idle" ? "" : state.tone}`} title={state.detail}>{state.label}</span>}
      <button type="button" className="icon-button" title="Remove from grid" aria-label={`Remove ${name} from grid`} onClick={onRemove}><Icon name="close" size={16} /></button>
      <Link className="button icon-button" to={`/terminals/${encodeURIComponent(id)}`} state={{ fromGrid: true }} title="Fullscreen: open the full terminal view" aria-label={`Fullscreen ${name}`}><Icon name="expand" size={18} /></Link>
    </header>
    {terminal ? <TerminalView sessionId={id} compact gridFontSize={fontSize} /> : <p className="grid-unavailable subtle">{loading ? "Loading terminal…" : "This session is no longer available. Remove it from the grid to choose another."}</p>}
  </section>;
}
