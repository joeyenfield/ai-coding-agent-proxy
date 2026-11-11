import { useLayoutEffect, useRef } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Icon } from "./Icon";

/** Where a list and its details stop fitting side by side (.split stacks there): the details then take the page. */
const STACKED = "(max-width: 1100px)";

/**
 * A list whose selected row's details sit beside it (Traffic, Requests). On a narrow screen the
 * details replace the list instead (.page.detail-open in styles.css), with a back button, and the
 * list comes back where it was scrolled to.
 *
 * The selection is the `param` search parameter, and selecting pushes a history entry, so the
 * browser's and Android's Back return to the list too.
 */
export function useDetailView(open: boolean, param = "request") {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const scroll = useRef<number | null>(null);

  // The list's scroll position is followed while it shows, not read when a row is picked: by the
  // time an effect runs the list is hidden and the page has already shrunk. A layout effect, so
  // the listener is gone before the shrinking page's own scroll event arrives.
  useLayoutEffect(() => {
    if (open) {
      if (window.matchMedia(STACKED).matches) window.scrollTo(0, 0);
      return;
    }
    if (scroll.current != null && window.matchMedia(STACKED).matches) window.scrollTo(0, scroll.current);
    const follow = () => {
      scroll.current = window.scrollY;
    };
    follow();
    window.addEventListener("scroll", follow, { passive: true });
    return () => window.removeEventListener("scroll", follow);
  }, [open]);

  const select = (key: string) => {
    const next = new URLSearchParams(params);
    next.set(param, key);
    setParams(next, { state: { fromList: true } });
  };
  // Opened from the list: go back to that entry. Opened by a link: there's no list entry to return to.
  const close = () => {
    if ((location.state as { fromList?: boolean } | null)?.fromList) {
      navigate(-1);
      return;
    }
    const next = new URLSearchParams(params);
    next.delete(param);
    setParams(next, { replace: true });
  };
  return { select, close };
}

/** The way back to the list, shown only where the details have replaced it. */
export function DetailBack({ label, onBack }: { label: string; onBack: () => void }) {
  return (
    <button type="button" className="detail-back" onClick={onBack}>
      <Icon name="chevron" size={16} />
      {label}
    </button>
  );
}
