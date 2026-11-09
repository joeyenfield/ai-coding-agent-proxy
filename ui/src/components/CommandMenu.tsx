import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { visiblePages } from "../navigation";
import { Icon, type IconName } from "./Icon";

interface Entry {
  key: string;
  to: string;
  label: string;
  hint?: string;
  group: string;
  icon: IconName;
}

/** Ctrl+K or ⌘K anywhere opens it; Escape closes. */
export function useCommandShortcut(open: () => void): void {
  useEffect(() => {
    const listen = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        open();
      }
    };
    window.addEventListener("keydown", listen);
    return () => window.removeEventListener("keydown", listen);
  }, [open]);
}

/** Go to any page you can use, or any machine you can see, by typing part of its name. */
export function CommandMenu({ open, onClose, siteAdmin, admin }: { open: boolean; onClose: () => void; siteAdmin: boolean; admin: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  // The same query the rail's count uses, so opening the menu costs nothing new.
  const machines = useQuery({ queryKey: ["machines"], queryFn: api.machines, enabled: open, retry: false });

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) {
      setQuery("");
      setActive(0);
      element.showModal();
      input.current?.focus();
    } else if (!open && element.open) {
      element.close();
    }
  }, [open]);

  const entries = useMemo<Entry[]>(() => {
    const pages: Entry[] = visiblePages(siteAdmin, admin).map(({ page, section }) => ({
      key: `page:${page.to}`, to: page.to, label: page.label, hint: page.hint, group: section, icon: page.icon ?? "chevron",
    }));
    const fleet: Entry[] = (machines.data?.machines ?? []).map((machine) => ({
      key: `machine:${machine.id}`, to: `/machines/${encodeURIComponent(machine.id)}`, label: machine.name,
      hint: machine.connected ? "Online" : "Offline", group: "Machines you can see", icon: "monitor",
    }));
    return [{ key: "page:/overview", to: "/overview", label: "Overview", hint: "How requests flow, and the server's numbers", group: "Home", icon: "home" }, ...pages, ...fleet];
  }, [siteAdmin, admin, machines.data]);

  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const shown = entries.filter((entry) => {
    const text = `${entry.label} ${entry.hint ?? ""} ${entry.group}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
  const chosen = Math.min(active, Math.max(shown.length - 1, 0));

  const go = (entry: Entry | undefined) => {
    if (!entry) return;
    onClose();
    navigate(entry.to);
  };

  let lastGroup = "";
  return (
    <dialog
      ref={dialog}
      className="command-menu"
      aria-label="Go to"
      onClose={onClose}
      onClick={(event) => event.target === dialog.current && onClose()}
    >
      <div className="command-box">
        <label className="command-search">
          <Icon name="search" size={18} />
          <input
            ref={input}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActive(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActive((index) => Math.min(index + 1, shown.length - 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActive((index) => Math.max(index - 1, 0));
              } else if (event.key === "Enter") {
                event.preventDefault();
                go(shown[chosen]);
              }
            }}
            placeholder="Go to a page or machine…"
            aria-label="Search pages and machines"
            aria-controls="command-results"
            aria-activedescendant={shown[chosen] ? `command-${chosen}` : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          <kbd>Esc</kbd>
        </label>
        <ul className="command-results" id="command-results" role="listbox" aria-label="Results">
          {shown.length === 0 && <li className="command-empty">Nothing matches “{query}”.</li>}
          {shown.map((entry, index) => {
            const heading = entry.group !== lastGroup ? entry.group : null;
            lastGroup = entry.group;
            return (
              <li key={entry.key} role="presentation">
                {heading && <p className="command-group" aria-hidden="true">{heading}</p>}
                <button
                  type="button"
                  id={`command-${index}`}
                  role="option"
                  aria-selected={index === chosen}
                  className="command-item"
                  onMouseMove={() => setActive(index)}
                  onClick={() => go(entry)}
                >
                  <span className="command-icon"><Icon name={entry.icon} size={16} /></span>
                  <span className="command-label">{entry.label}</span>
                  {entry.hint && <span className="command-hint">{entry.hint}</span>}
                </button>
              </li>
            );
          })}
        </ul>
        <p className="command-foot subtle">
          <span><kbd>↑</kbd> <kbd>↓</kbd> to move</span>
          <span><kbd>Enter</kbd> to open</span>
        </p>
      </div>
    </dialog>
  );
}
