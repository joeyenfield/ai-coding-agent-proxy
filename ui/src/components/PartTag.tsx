import { Link, useLocation } from "react-router-dom";
import { sectionFor } from "../navigation";
import { Icon } from "./Icon";

/** "Activity › Requests" above a page title: the rail's section this page is in, and the page when you're deeper in it. */
export function PartTag() {
  const { pathname } = useLocation();
  const found = sectionFor(pathname);
  if (!found) return null;
  const { section, page } = found;
  const home = section.items[0]?.to.split("#")[0];
  const deeper = pathname !== page.to;
  return (
    <nav className={`part-tag section-${section.id}`} aria-label="Breadcrumb">
      {home && pathname !== home ? <Link to={home}>{section.label}</Link> : <span>{section.label}</span>}
      {deeper && (
        <>
          <Icon name="chevron" size={12} className="part-sep" />
          <Link to={page.to}>{page.label}</Link>
        </>
      )}
    </nav>
  );
}

/** The numbered dot used for a part on the Overview and the routing map. */
export function PartNumber({ value }: { value: number }) {
  return (
    <span className="part-number" aria-hidden="true">
      {value}
    </span>
  );
}
