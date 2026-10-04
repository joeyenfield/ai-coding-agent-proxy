import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isFailure } from "../api";
import { formatCompact, formatDate, formatNumber, formatRelative, lastTraffic } from "../format";
import { useRequests, useStatus, useToast } from "../hooks";
import { useLive, useTicker } from "../live";
import { RouteBadge } from "../components/RouteBadge";
import { RequestList } from "../components/RequestList";
import { groupRepeats, mergeRows } from "../traffic";

/** One session: its live traffic as it streams, then its saved request history. */
export function SessionPage() {
  const { sessionId = "" } = useParams();
  const session = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.session(sessionId), refetchInterval: 3000 });
  const live = useLive(sessionId);
  const requests = useRequests();
  const status = useStatus();
  const client = useQueryClient();
  const notify = useToast();
  useTicker(live.active.length > 0 || Boolean(session.data && !session.data.ended_at), 1000);

  const history = useMemo(() => (requests.data ?? []).filter((record) => record.session_id === sessionId), [requests.data, sessionId]);
  const [modelOnly, setModelOnly] = useState(false);
  const [grouped, setGrouped] = useState(true);
  const rows = useMemo(() => {
    const merged = mergeRows(history, [...live.recent, ...live.active]).filter((row) => !modelOnly || row.category === "model");
    return grouped ? groupRepeats(merged) : merged;
  }, [history, live.active, live.recent, modelOnly, grouped]);
  const totals = useMemo(
    () => ({
      input: history.reduce((total, record) => total + (record.input_tokens || 0), 0),
      output: history.reduce((total, record) => total + (record.output_tokens || 0), 0),
      failed: history.filter(isFailure).length,
    }),
    [history],
  );
  const trace = useMutation({
    mutationFn: (enabled: boolean) => api.setTrace(sessionId, enabled),
    onSuccess: (_, enabled) => {
      notify(enabled ? "Tracing enabled for future requests" : "Tracing disabled");
      client.invalidateQueries({ queryKey: ["session", sessionId] });
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => notify(error.message),
  });

  if (session.isError) {
    return (
      <div className="page">
        <header className="page-head">
          <h1>Session not found</h1>
          <p className="lede">
            It may have been deleted. <Link to="/sessions">Back to sessions</Link>
          </p>
        </header>
      </div>
    );
  }
  if (!session.data) return <p className="empty">Loading the session…</p>;
  const data = session.data;
  const streaming = live.active.length;
  const proxyUrl = status.data?.proxy_url ?? "";

  return (
    <div className="page wide">
      <header className="page-head">
        <p>
          <Link to="/sessions">Sessions</Link>
        </p>
        <h1>
          {data.client}
          {data.project && <span className="subtle"> in {data.project}</span>}
        </h1>
        <p className="lede">
          {data.model || "Model chosen by the agent"} via <RouteBadge item={data} />.{" "}
          {data.ended_at
            ? `Ended ${formatRelative(data.ended_at)}${data.exit_status == null ? "" : ` with exit code ${data.exit_status}`}.`
            : `Open since ${formatRelative(data.started_at)}.`}
        </p>
      </header>

      <dl className="totals">
        <div>
          <dt>Last traffic</dt>
          <dd>{streaming ? <span className="warn">Streaming now</span> : lastTraffic(data)}</dd>
        </div>
        <div>
          <dt>Requests</dt>
          <dd>
            {formatNumber(data.request_count)}
            {totals.failed > 0 && <small className="bad"> {totals.failed} failed</small>}
          </dd>
        </div>
        <div>
          <dt>Tokens read</dt>
          <dd>{formatCompact(totals.input)}</dd>
        </div>
        <div>
          <dt>Tokens written</dt>
          <dd>{formatCompact(totals.output)}</dd>
        </div>
      </dl>

      <section className="section">
        <div className="section-head">
          <h2>{streaming ? `Requests (${streaming} streaming)` : "Requests"}</h2>
          <span className={`connection ${live.connected ? "" : "offline"}`}>
            <span className="dot" aria-hidden="true" />
            {live.connected ? (data.ended_at ? "Session ended" : "Listening for new requests") : "Reconnecting…"}
          </span>
        </div>
        <div className="filters">
          <label className="check">
            <input type="checkbox" checked={modelOnly} onChange={(event) => setModelOnly(event.target.checked)} />
            Model calls only
          </label>
          <label className="check">
            <input type="checkbox" checked={grouped} onChange={(event) => setGrouped(event.target.checked)} />
            Group repeats
          </label>
          <Link to={`/traffic?session=${encodeURIComponent(sessionId)}`}>Open in Traffic</Link>
        </div>
        {rows.length ? (
          <RequestList key={sessionId} rows={rows} />
        ) : (
          <p className="empty">
            {data.ended_at
              ? "No requests were recorded in this session."
              : "Nothing yet. Requests appear here as the agent sends them."}
          </p>
        )}
      </section>

      <details className="section collapsible">
        <summary>
          <h2>Details</h2>
        </summary>
        <dl className="endpoints">
          <dt>Session</dt>
          <dd className="mono">{data.session_id}</dd>
          <dt>Started</dt>
          <dd>{formatDate(data.started_at)}</dd>
          <dt>Payloads</dt>
          <dd>
            <label className="check">
              <input type="checkbox" checked={data.trace} disabled={trace.isPending} onChange={(event) => trace.mutate(event.target.checked)} />
              Capture full request and response payloads
            </label>
          </dd>
          {proxyUrl && (
            <>
              <dt>OpenAI URL</dt>
              <dd className="mono">{`${proxyUrl}/session/${data.session_id}/v1`}</dd>
              <dt>Anthropic URL</dt>
              <dd className="mono">{`${proxyUrl}/session/${data.session_id}/anthropic`}</dd>
            </>
          )}
        </dl>
      </details>
    </div>
  );
}
