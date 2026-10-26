import { Link, useLocation } from "react-router-dom";
import { partFor } from "../navigation";

/** "LLM providers › Ollama" above a page title: which of the console's parts this page is in. */
export function PartTag() {
  const { pathname } = useLocation();
  const found = partFor(pathname);
  if (!found) return null;
  const { part, group } = found;
  return (
    <p className={`part-tag part-${part.id}`}>
      {part.home && pathname !== part.home ? <Link to={part.home}>{part.label}</Link> : part.label}
      {group && (
        <>
          <span className="part-sep" aria-hidden="true">
            ›
          </span>
          {group}
        </>
      )}
    </p>
  );
}

/** The numbered dot used for a part in the rail, the Overview and the routing map. */
export function PartNumber({ value }: { value: number }) {
  return (
    <span className="part-number" aria-hidden="true">
      {value}
    </span>
  );
}
