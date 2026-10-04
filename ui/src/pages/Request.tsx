import { Link, useParams } from "react-router-dom";
import { requestKey } from "../api";
import { Inspector } from "../components/Inspector";
import { useRequests, useSessions } from "../hooks";
import { useLive, useTicker } from "../live";
import { classify, CATEGORY_LABELS } from "../traffic";
import { LiveCard } from "./Live";

/** One request on its own screen: streaming as a live card, then its saved timing and payload. */
export function RequestPage() {
  const { sessionId = "", requestId = "" } = useParams();
  const requests = useRequests();
  const sessions = useSessions();
  const live = useLive(sessionId);
  const key = `${sessionId}:${requestId}`;
  const record = requests.data?.find((item) => requestKey(item) === key);
  const active = live.active.find((entry) => entry.key === key);
  const session = sessions.data?.find((item) => item.session_id === sessionId);
  useTicker(Boolean(active), 500);
  const subject = record ?? active;
  const label = subject
    ? classify({ kind: subject.kind ?? "api", host: subject.host ?? "", path: subject.endpoint, model: subject.model })
    : null;

  return (
    <div className="page">
      <header className="page-head">
        <p className="breadcrumbs">
          <Link to="/sessions">Sessions</Link>
          <span aria-hidden="true"> › </span>
          <Link to={`/sessions/${encodeURIComponent(sessionId)}`}>{session?.client ?? "Session"}</Link>
          <span aria-hidden="true"> › </span>
          <span>Request {requestId}</span>
        </p>
        <h1>{subject?.model || (label ? `${CATEGORY_LABELS[label.category]}: ${label.service}` : `Request ${requestId}`)}</h1>
      </header>
      {active ? (
        <LiveCard entry={active} />
      ) : record ? (
        <Inspector record={record} session={session} />
      ) : requests.isPending ? (
        <p className="empty">Loading the request…</p>
      ) : (
        <p className="empty">
          This request isn't in the history. It may have been cleared. <Link to={`/sessions/${encodeURIComponent(sessionId)}`}>Back to the session</Link>
        </p>
      )}
    </div>
  );
}
