import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isFailure, outcomeLabel, requestKey, tokens } from "../api";
import { endpointPath, formatCompact, formatDate, formatDuration, formatNumber, formatRelative, lastTraffic } from "../format";
import { useRequests, useStatus, useToast } from "../hooks";
import { useLive, useTicker } from "../live";
import { LiveCard } from "./Live";

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
  const finishedHere = live.recent;
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
          {data.model || "Model chosen by the agent"} on {data.backend}.{" "}
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
          <h2>{streaming ? `Streaming now (${streaming})` : "Live traffic"}</h2>
          <span className={`connection ${live.connected ? "" : "offline"}`}>
            <span className="dot" aria-hidden="true" />
            {live.connected ? "Listening for this session's requests" : "Reconnecting…"}
          </span>
        </div>
        {streaming ? (
          <div className={`live-grid ${streaming > 1 ? "multi" : ""}`}>
            {live.active.map((entry) => (
              <LiveCard key={entry.key} entry={entry} />
            ))}
          </div>
        ) : (
          <p className="empty">
            {data.ended_at
              ? "This session has ended, so no new traffic will arrive."
              : "Nothing is streaming in this session right now. New requests appear here as they start."}
          </p>
        )}
        {finishedHere.length > 0 && (
          <>
            <h3>Finished while you've been watching</h3>
            <div className="live-grid multi">
              {finishedHere.map((entry) => (
                <LiveCard key={entry.key} entry={entry} />
              ))}
            </div>
          </>
        )}
      </section>

      <section className="section">
        <div className="section-head">
          <h2>Request history</h2>
          <Link to={`/requests?session=${encodeURIComponent(sessionId)}`}>Inspect in request history</Link>
        </div>
        {history.length ? (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col" className="num">First token</th>
                  <th scope="col" className="num">Duration</th>
                  <th scope="col" className="num">Output</th>
                  <th scope="col" className="num">When</th>
                </tr>
              </thead>
              <tbody>
                {history.slice(0, 25).map((record) => (
                  <tr key={requestKey(record)}>
                    <td>
                      <span className="title-line">
                        <Link to={`/requests?request=${encodeURIComponent(requestKey(record))}`}>
                          #{record.request_id} {record.model || "Unknown model"}
                        </Link>
                        {isFailure(record) && <span className="pill bad">{outcomeLabel(record)}</span>}
                      </span>
                      <small className="mono">{endpointPath(record.endpoint)}</small>
                    </td>
                    <td className="num">{formatDuration(record.ttft_ms)}</td>
                    <td className="num">{formatDuration(record.total_ms)}</td>
                    <td className="num nowrap">
                      {tokens(record, record.output_tokens)} tok
                      <small>{record.generation_tps == null ? "–" : `${formatNumber(record.generation_tps)}/s`}</small>
                    </td>
                    <td className="num subtle" title={formatDate(record.timestamp)}>
                      {formatRelative(record.timestamp)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty">No requests have been recorded in this session.</p>
        )}
      </section>

      <section className="section">
        <h2>Details</h2>
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
      </section>
    </div>
  );
}
