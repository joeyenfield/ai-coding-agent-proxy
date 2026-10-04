import type { ReactNode } from "react";

/**
 * One line in a single-column list: collapsed to a summary, expandable in place.
 * Actions sit outside the toggle so links like "Open" don't expand the row.
 */
export function ExpandRow({
  expanded,
  onToggle,
  summary,
  actions,
  children,
  tone,
}: {
  expanded: boolean;
  onToggle: () => void;
  summary: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  tone?: "live" | "bad";
}) {
  return (
    <li className={`xrow ${expanded ? "is-open" : ""} ${tone ? `is-${tone}` : ""}`}>
      <div className="xrow-head">
        <button type="button" className="xrow-toggle" aria-expanded={expanded} onClick={onToggle}>
          <span className="xrow-caret" aria-hidden="true">
            ▸
          </span>
          <span className="xrow-summary">{summary}</span>
        </button>
        {actions && <div className="xrow-actions">{actions}</div>}
      </div>
      {expanded && <div className="xrow-body">{children}</div>}
    </li>
  );
}
