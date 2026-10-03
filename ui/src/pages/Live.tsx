import { useLayoutEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { requestKey } from "../api";
import { formatDuration, formatNumber, formatRelative } from "../format";
import { liveRate, phaseOf, useLive, useTicker, type LiveRequest, type Phase } from "../live";

const PHASE_LABELS: Record<Phase, string> = {
  waiting: "Waiting for first token",
  thinking: "Thinking",
  answering: "Writing",
  tools: "Calling tools",
  done: "Finished",
};

export function LivePage() {
  const live = useLive();
  useTicker(live.active.length > 0);
  return (
    <div className="page wide">
      <header className="page-head">
        <h1>
          {live.active.length
            ? `Watching ${live.active.length} ${live.active.length === 1 ? "request" : "requests"}`
            : "Nothing streaming right now"}
        </h1>
        <p className="lede">
          Tokens appear here as the model produces them. Reasoning, the reply and tool calls are shown separately.
          {!live.connected && <span className="bad"> Reconnecting to the proxy…</span>}
        </p>
        {!live.enabled && (
          <p className="banner warn">
            Live text is turned off on the proxy (AI_PROXY_LIVE=0). Token counts still update.
          </p>
        )}
      </header>

      {live.active.length > 0 ? (
        <div className={`live-grid ${live.active.length > 1 ? "multi" : ""}`}>
          {live.active.map((entry) => (
            <LiveCard key={entry.key} entry={entry} />
          ))}
        </div>
      ) : (
        <div className="empty">
          <p>Start an agent and its requests will stream in here.</p>
          <Link className="button primary" to="/agents">
            Choose an agent
          </Link>
        </div>
      )}

      {live.recent.length > 0 && (
        <section className="section">
          <h2>Just finished</h2>
          <div className="live-grid multi">
            {live.recent.map((entry) => (
              <LiveCard key={entry.key} entry={entry} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export function LiveCard({ entry }: { entry: LiveRequest }) {
  const phase = phaseOf(entry);
  const now = entry.done && entry.ended_at ? entry.ended_at : Date.now() / 1000;
  const elapsed = (now - entry.started_at) * 1000;
  const firstToken = entry.first_token_at ? (entry.first_token_at - entry.started_at) * 1000 : null;
  const rate = entry.done ? entry.record?.generation_tps ?? null : liveRate(entry);
  const tokens = entry.done && entry.record ? entry.record.output_tokens : entry.chunks.reasoning + entry.chunks.content + entry.chunks.tool;
  const [showReasoning, setShowReasoning] = useState(true);
  const hasReasoning = entry.reasoning.length > 0 || entry.chunks.reasoning > 0;

  return (
    <article className={`live-card phase-${phase}`} aria-label={`${entry.client} using ${entry.model}`}>
      <header className="live-card-head">
        <div>
          <h2>{entry.model || "Unknown model"}</h2>
          <p className="subtle">
            {entry.client} on {entry.backend}
          </p>
        </div>
        <span className={`phase phase-${phase}`}>
          {phase !== "done" && <span className="live-mark" aria-hidden="true" />}
          {PHASE_LABELS[phase]}
        </span>
      </header>
      <dl className="live-stats">
        <div>
          <dt>{entry.done ? "Took" : "Elapsed"}</dt>
          <dd>{formatDuration(elapsed)}</dd>
        </div>
        <div>
          <dt>First token</dt>
          <dd>{firstToken == null ? "–" : formatDuration(firstToken)}</dd>
        </div>
        <div>
          <dt>{entry.done ? "Tokens" : "Tokens so far"}</dt>
          <dd>{entry.done ? formatNumber(tokens) : `≈ ${formatNumber(tokens)}`}</dd>
        </div>
        <div>
          <dt>Speed</dt>
          <dd>{rate == null ? "–" : `${formatNumber(rate)} tok/s`}</dd>
        </div>
      </dl>
      {hasReasoning && (
        <section className="stream-pane reasoning">
          <button type="button" className="pane-toggle" aria-expanded={showReasoning} onClick={() => setShowReasoning(!showReasoning)}>
            Reasoning <span className="subtle">≈ {formatNumber(entry.chunks.reasoning)} tokens</span>
          </button>
          {showReasoning && <StreamText text={entry.reasoning} live={phase === "thinking"} />}
        </section>
      )}
      {entry.tool && (
        <section className="stream-pane tool">
          <h3>Tool calls</h3>
          <StreamText text={entry.tool} live={phase === "tools"} mono />
        </section>
      )}
      <section className="stream-pane answer">
        <h3>Reply</h3>
        {entry.content ? (
          <StreamText text={entry.content} live={phase === "answering"} />
        ) : (
          <p className="subtle">{phase !== "done" ? "Nothing yet." : entry.tool ? "No reply text; see the tool calls above." : "No reply text."}</p>
        )}
      </section>
      <footer className="live-card-foot subtle">
        <span>
          Request {entry.request_id} {entry.done && entry.ended_at ? `finished ${formatRelative(new Date(entry.ended_at * 1000).toISOString())}` : ""}
        </span>
        {entry.done && (
          <Link to={`/requests?request=${encodeURIComponent(requestKey(entry))}`}>Open in request history</Link>
        )}
      </footer>
    </article>
  );
}

/** Text that keeps itself scrolled to the newest token unless the reader scrolls up. */
function StreamText({ text, live, mono = false }: { text: string; live: boolean; mono?: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useLayoutEffect(() => {
    if (box.current && pinned.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text]);
  return (
    <div
      ref={box}
      className={`stream-text ${mono ? "mono" : ""}`}
      onScroll={(event) => {
        const element = event.currentTarget;
        pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
      }}
    >
      {text}
      {live && <span className="caret" aria-hidden="true" />}
    </div>
  );
}
