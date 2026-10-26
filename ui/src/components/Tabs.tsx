import { useRef, type KeyboardEvent, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

export interface TabItem<T extends string> {
  id: T;
  label: ReactNode;
  /** Shown after the label: a count, progress, or an unsaved-changes dot. */
  badge?: ReactNode;
}

const slug = (value: string) => value.replace(/[^a-z0-9]+/gi, "-");
const tabId = (prefix: string, id: string) => `${prefix}tab-${slug(id)}`;
const panelId = (prefix: string, id: string) => `${prefix}panel-${slug(id)}`;

/** The active tab, kept in the URL so reloads and links land on it. The first tab is the default and stays out of the URL. */
export function useTabParam<T extends string>(ids: readonly T[], param = "tab"): [T, (id: T) => void] {
  const [params, setParams] = useSearchParams();
  const value = params.get(param);
  const active = ids.find((id) => id === value) ?? ids[0];
  const select = (id: T) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (id === ids[0]) next.delete(param);
        else next.set(param, id);
        return next;
      },
      { replace: true },
    );
  return [active, select];
}

/** An ARIA tab list: arrow keys, Home and End move between tabs. Vertical lists render as a side menu. */
export function TabBar<T extends string>({
  label,
  tabs,
  active,
  onSelect,
  idPrefix,
  vertical = false,
  className = "",
}: {
  label: string;
  tabs: TabItem<T>[];
  active: T;
  onSelect: (id: T) => void;
  idPrefix: string;
  vertical?: boolean;
  className?: string;
}) {
  const list = useRef<HTMLDivElement>(null);
  const onKeyDown = (event: KeyboardEvent) => {
    const [previous, following] = vertical ? ["ArrowUp", "ArrowDown"] : ["ArrowLeft", "ArrowRight"];
    const index = tabs.findIndex((tab) => tab.id === active);
    const next =
      event.key === previous ? (index - 1 + tabs.length) % tabs.length
      : event.key === following ? (index + 1) % tabs.length
      : event.key === "Home" ? 0
      : event.key === "End" ? tabs.length - 1
      : -1;
    if (next < 0) return;
    event.preventDefault();
    onSelect(tabs[next].id);
    list.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  };
  return (
    <div
      ref={list}
      className={`${vertical ? "subnav" : "tabs"} ${className}`}
      role="tablist"
      aria-label={label}
      aria-orientation={vertical ? "vertical" : "horizontal"}
      onKeyDown={onKeyDown}
    >
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          id={tabId(idPrefix, tab.id)}
          aria-controls={panelId(idPrefix, tab.id)}
          aria-selected={tab.id === active}
          tabIndex={tab.id === active ? 0 : -1}
          onClick={() => onSelect(tab.id)}
        >
          {tab.label}
          {tab.badge}
        </button>
      ))}
    </div>
  );
}

/** Inactive panels render nothing, so their polling stops; keepMounted holds on to state such as search results. */
export function TabPanel({
  idPrefix,
  id,
  active,
  keepMounted = false,
  className,
  children,
}: {
  idPrefix: string;
  id: string;
  active: boolean;
  keepMounted?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div role="tabpanel" id={panelId(idPrefix, id)} aria-labelledby={tabId(idPrefix, id)} hidden={!active} className={className}>
      {active || keepMounted ? children : null}
    </div>
  );
}

export function TabCount({ value, tone }: { value: ReactNode; tone?: "live" }) {
  return <span className={`tab-badge ${tone ?? ""}`}>{value}</span>;
}

export function UnsavedDot() {
  return (
    <span className="tab-dot" title="Unsaved changes">
      <span className="visually-hidden">(unsaved changes)</span>
    </span>
  );
}
