import { useEffect, useId, useRef, useState } from "react";
import type { Agent, FleetMachine } from "../api";
import { ROUTE_ORDER, ROUTES } from "../routes";

/** A styled list keeps missing agents legible and selectable for their install instructions. */
export function AgentPicker({ agents, machine, value, loading, onChange }: {
  agents: Agent[]; machine?: FleetMachine; value: string; loading: boolean; onChange: (id: string) => void;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    (list.current?.querySelector<HTMLButtonElement>('[aria-selected="true"]') ?? list.current?.querySelector<HTMLButtonElement>('[role="option"]'))?.focus();
    const closeOutside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);
  const selected = agents.find((agent) => agent.id === value);
  const missing = (agent: Agent) => Boolean(machine && !machine.agents.includes(agent.id));
  const groups = ROUTE_ORDER.map((route) => ({
    route, agents: agents.filter((agent) => (agent.route ?? (agent.intercept ? "account" : "ollama")) === route),
  })).filter((group) => group.agents.length);
  const focusOption = (index: number) => {
    const options = list.current?.querySelectorAll<HTMLButtonElement>('[role="option"]');
    if (options?.length) options[(index + options.length) % options.length].focus();
  };
  return (
    <div ref={container} className="field agent-picker" onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <span id={`${id}-label`} className="start-step">2. Which agent?</span>
      <button
        ref={trigger} type="button" className="agent-picker-trigger" disabled={!agents.length}
        aria-labelledby={`${id}-label ${id}-value`} aria-haspopup="listbox" aria-expanded={open} aria-controls={`${id}-list`}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span id={`${id}-value`}>
          <span className={selected && missing(selected) ? "agent-missing" : undefined}>
            {selected?.name ?? (loading ? "Loading agents…" : "Choose an agent…")}
          </span>
          {selected && missing(selected) && <span className="subtle"> — Not installed</span>}
        </span>
        <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div ref={list} id={`${id}-list`} className="agent-picker-list" role="listbox" aria-labelledby={`${id}-label`}
          onKeyDown={(event) => {
            const options = Array.from(list.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? []);
            const index = options.indexOf(document.activeElement as HTMLButtonElement);
            if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
              event.preventDefault();
              focusOption(event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : index + (event.key === "ArrowDown" ? 1 : -1));
            } else if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
              trigger.current?.focus();
            } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && event.key !== " ") {
              const next = [...options.slice(index + 1), ...options.slice(0, index + 1)].find((option) => option.textContent?.trim().toLowerCase().startsWith(event.key.toLowerCase()));
              next?.focus();
            }
          }}>
          {groups.map((group) => (
            <div key={group.route} role="group" aria-labelledby={`${id}-${group.route}`}>
              <div id={`${id}-${group.route}`} className="agent-picker-group">{ROUTES[group.route].label}</div>
              {group.agents.map((agent) => (
                <button key={agent.id} type="button" role="option" aria-selected={agent.id === value} tabIndex={-1}
                  onClick={() => { onChange(agent.id); setOpen(false); trigger.current?.focus(); }}>
                  <span className={missing(agent) ? "agent-missing" : undefined}>{agent.name}</span>
                  {missing(agent) && <small>Not installed</small>}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
      <small>{machine
        ? `${agents.filter((agent) => !missing(agent)).length} of ${agents.length} installed on ${machine.name}. Choose a crossed-out agent to see how to install it.`
        : "Agents signed in to your own plan, or running on your machines' models or a hosted API."}</small>
    </div>
  );
}
