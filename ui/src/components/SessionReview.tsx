import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, preferredModel, providerModels, type ModelTier, type ReviewFinding, type Session, type SessionReview as Review } from "../api";
import { formatCompact, formatDate, formatDuration, formatNumber, formatUsd } from "../format";
import { useModels, useToast } from "../hooks";
import { reviewBusy, startReviewRun, stopReviewRun, useReviewPhase, useReviewRun, useReviewRunId } from "../reviewRuns";
import { providerLabel } from "../routes";
import { Prose } from "./SessionChat";

const SEVERITY: Record<ReviewFinding["severity"], { label: string; pill: string }> = {
  high: { label: "High", pill: "pill bad" },
  medium: { label: "Medium", pill: "pill accent" },
  low: { label: "Low", pill: "pill" },
  info: { label: "Note", pill: "pill" },
};
// Models write a heading and its text without a blank line between; Prose takes a heading only as a paragraph of its own.
const spaced = (text: string) => text.replace(/^(#{1,6}\s.*)$/gm, "\n\n$1\n\n").trim();

const AREA: Record<ReviewFinding["area"], string> = {
  context: "Context",
  tools: "Tools",
  flow: "How it went",
  reliability: "Reliability",
  cost: "Cost",
};

const TIER: Record<ModelTier, { label: string; pill: string; color: string }> = {
  frontier: { label: "Frontier", pill: "pill bad", color: "var(--bad)" },
  mid: { label: "Mid-range", pill: "pill accent", color: "var(--accent)" },
  small: { label: "Small", pill: "pill good", color: "var(--good)" },
  local: { label: "Local", pill: "pill good", color: "var(--series-3)" },
  unpriced: { label: "No price", pill: "pill", color: "var(--line-strong)" },
};

const GRADE_CLASS: Record<string, string> = { Efficient: "good", "Some waste": "fair", Wasteful: "poor", "Very wasteful": "bad" };

const jump = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });

/**
 * The Review tab of a session: what the server's checks found in it (measured there, never here), the
 * numbers behind them, and an AI review by a model you pick. The AI review is a session of its own, sent
 * through the server like any model call: the server writes its messages, this page only carries them.
 * A review under way lives in reviewRuns.ts, not here, so leaving the tab doesn't stop it.
 */
export function SessionReview({ session }: { session: Session }) {
  const id = session.session_id;
  const review = useQuery({ queryKey: ["session-review", id], queryFn: () => api.sessionReview(id), retry: false });
  const models = useModels();
  const client = useQueryClient();
  const notify = useToast();

  // The backends the server says can review, with the models each has now.
  const choices = useMemo(() => {
    const result: { backend: string; type: string; models: string[] }[] = [];
    for (const backend of review.data?.reviewers ?? []) {
      const listing = models.data?.backends[backend];
      if (!listing?.online) continue;
      const names = providerModels(listing, models.data?.aliases, listing.type);
      if (names.length) result.push({ backend, type: listing.type ?? "ollama", models: names });
    }
    return result;
  }, [review.data?.reviewers, models.data]);
  const [picked, setPicked] = useState<{ backend: string; model: string } | null>(null);
  const choice = picked ?? (choices[0] ? { backend: choices[0].backend, model: preferredModel(choices[0].models) } : null);

  const [focus, setFocus] = useState("");
  const prompt = review.data?.prompts.find((item) => item.id === focus) ?? review.data?.prompts[0];
  const [question, setQuestion] = useState("");
  const [shown, setShown] = useState<string | null>(null);
  // Only the run's phase and session here: its text changes with every word, and LiveReview reads that.
  const phase = useReviewPhase(id);
  const runId = useReviewRunId(id);
  const busy = phase === "starting" || phase === "waiting" || phase === "thinking" || phase === "writing";
  const past = useQuery({
    queryKey: ["session-review-text", id, shown],
    queryFn: () => api.readReview(id, shown!),
    enabled: Boolean(shown),
    retry: false,
  });

  const [area, setArea] = useState<ReviewFinding["area"] | "all">("all");
  // Which findings are open, where someone chose; the rest follow their level (FindingList).
  const [opened, setOpened] = useState<Record<string, boolean>>({});

  const start = () => {
    if (!choice || !prompt || busy) return;
    setShown(null);
    void startReviewRun(id, choice, prompt, question, client, notify);
  };

  if (review.isError) return <p className="bad">{review.error.message}</p>;
  if (!review.data) return <p className="empty">Checking the session…</p>;
  const { traffic, prompt: sent, tools, findings, reviews, prompts, score, grade, checks } = review.data;
  if (!traffic.model_calls) return <p className="empty">No model calls yet. A review needs some: come back once the agent has talked to its model.</p>;
  const value = choice ? `${choice.backend}\n${choice.model}` : "";
  // A note (payloads not captured) isn't a finding against the session.
  const counted = findings.filter((item) => item.severity !== "info");
  const levels = (["high", "medium", "low"] as const).map((level) => ({ level, count: counted.filter((item) => item.severity === level).length })).filter((item) => item.count);
  const impact = findings.reduce((sum, item) => sum + (item.impact_tokens ?? 0), 0);
  const areas = (Object.keys(AREA) as ReviewFinding["area"][]).map((name) => ({ name, count: findings.filter((item) => item.area === name).length })).filter((item) => item.count);
  const listed = area === "all" ? findings : findings.filter((item) => item.area === area);
  const isOpen = (finding: ReviewFinding, index: number) => opened[finding.id] ?? (finding.severity === "high" || index === 0);
  const allOpen = listed.every((finding, index) => isOpen(finding, index));
  const cost = traffic.usd + traffic.subscription_usd;
  const frontier = traffic.tiers.find((item) => item.tier === "frontier");

  return (
    <div className="review">
      <section className={`review-verdict grade-${GRADE_CLASS[grade ?? ""] ?? "none"}`} aria-label="The review in short">
        {score != null && (
          <div className="review-score" title="100, less 18 for each high finding, 9 for each medium and 3 for each low">
            <strong>{score}</strong>
            <span>of 100</span>
          </div>
        )}
        <div className="review-verdict-text">
          <h2>{grade ?? "Not scored"}</h2>
          <p>
            {counted.length ? (
              <>
                {counted.length} of {checks} checks found something: {levels.map((item) => `${item.count} ${item.level}`).join(", ")}.
                {impact > 0 && ` The ones that can say what they cost add up to about ${formatCompact(impact)} tokens.`}
              </>
            ) : (
              `None of the ${checks} checks found anything to change.`
            )}
          </p>
          <nav className="chip-bar" aria-label="Parts of the review">
            <button type="button" onClick={() => jump("review-checks")}>
              Checks{findings.length ? ` (${findings.length})` : ""}
            </button>
            <button type="button" onClick={() => jump("review-measured")}>
              What was measured
            </button>
            <button type="button" onClick={() => jump("review-ai")}>
              AI review{busy ? " (running)" : reviews.length ? ` (${reviews.length})` : ""}
            </button>
          </nav>
        </div>
      </section>

      <dl className="stat-cards review-stats" aria-label="What the review measured">
        <div>
          <dt>Model calls</dt>
          <dd>
            {formatNumber(traffic.model_calls)}
            {traffic.duration_seconds > 0 && <small> in {formatDuration(traffic.duration_seconds * 1000)}</small>}
          </dd>
        </div>
        <div>
          <dt>Tokens read</dt>
          <dd>
            {formatCompact(traffic.input_tokens)}
            {traffic.cached_share != null && <small> {Math.round(traffic.cached_share * 100)}% cached</small>}
          </dd>
        </div>
        <div>
          <dt>Largest call</dt>
          <dd>
            {formatCompact(traffic.peak_context_tokens)}
            {traffic.context_window ? <small> of {formatCompact(traffic.context_window)}</small> : null}
          </dd>
        </div>
        {sent && (
          <div>
            <dt>System prompt</dt>
            <dd>
              {formatCompact(sent.system_tokens)}
              <small> tokens</small>
            </dd>
          </div>
        )}
        {tools && (
          <div>
            <dt>Tool calls</dt>
            <dd>
              {formatNumber(tools.calls)}
              {tools.errors > 0 && <small className="bad"> {formatNumber(tools.errors)} failed</small>}
            </dd>
          </div>
        )}
        {cost > 0 && (
          <div>
            <dt>{traffic.usd === 0 ? "Cost at API prices" : "Estimated cost"}</dt>
            <dd title={`${formatUsd(traffic.usd)} paid by the token, ${formatUsd(traffic.subscription_usd)} at API prices on a subscription`}>{formatUsd(cost)}</dd>
          </div>
        )}
        {traffic.tiers.length > 0 && (
          <div>
            <dt>Frontier models</dt>
            <dd>
              {Math.round((frontier?.share ?? 0) * 100)}%<small> of calls</small>
            </dd>
          </div>
        )}
      </dl>

      <section className="review-part" id="review-checks">
        <div className="section-head">
          <h2>Checks</h2>
          <div className="review-actions">
            {listed.length > 1 && (
              <button type="button" className="small" onClick={() => setOpened(Object.fromEntries(listed.map((finding) => [finding.id, !allOpen])))}>
                {allOpen ? "Collapse all" : "Expand all"}
              </button>
            )}
            <button type="button" className="small" onClick={() => review.refetch()} disabled={review.isFetching}>
              {review.isFetching ? "Checking…" : "Check again"}
            </button>
          </div>
        </div>
        {areas.length > 1 && (
          <div className="chip-bar" role="group" aria-label="Findings by area">
            <button type="button" aria-pressed={area === "all"} onClick={() => setArea("all")}>
              All ({findings.length})
            </button>
            {areas.map((item) => (
              <button key={item.name} type="button" aria-pressed={area === item.name} onClick={() => setArea(item.name)}>
                {AREA[item.name]} ({item.count})
              </button>
            ))}
          </div>
        )}
        {findings.length ? (
          <ul className="review-findings">
            {listed.map((finding, index) => (
              <li key={finding.id}>
                <details
                  className={`review-finding severity-${finding.severity}`}
                  open={isOpen(finding, index)}
                  onToggle={(event) => {
                    const now = event.currentTarget.open;
                    if (now !== isOpen(finding, index)) setOpened((current) => ({ ...current, [finding.id]: now }));
                  }}
                >
                  <summary className="review-finding-head">
                    <span className={SEVERITY[finding.severity].pill}>{SEVERITY[finding.severity].label}</span>
                    <strong>{finding.title}</strong>
                    <span className="subtle">
                      {finding.impact_tokens ? `about ${formatCompact(finding.impact_tokens)} tokens · ` : ""}
                      {AREA[finding.area]}
                    </span>
                  </summary>
                  <p>{finding.detail}</p>
                  <p className="review-suggestion">
                    <strong>Try:</strong> {finding.suggestion}
                  </p>
                </details>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty">Nothing stood out. The checks look at the context's size, how tools were used, how the session went, failures, caching and which models did the work.</p>
        )}
      </section>

      <Evidence review={review.data} />

      <section className="review-part" id="review-ai">
        <h2>AI review</h2>
        <p className="subtle">
          A model reads what was measured and suggests what to change. Choose what it should look at: each review has its own
          question and is sent only the data it needs, never the whole transcript. It runs as a private session of your own.
        </p>
        <div className="chip-bar" role="group" aria-label="What to review">
          {prompts.map((item) => (
            <button key={item.id} type="button" aria-pressed={item.id === prompt?.id} disabled={busy} onClick={() => setFocus(item.id)}>
              {item.title}
            </button>
          ))}
        </div>
        {prompt && (
          <details className="review-prompt">
            <summary>{prompt.about}</summary>
            <dl>
              <dt>Data sent</dt>
              <dd>{prompt.data.join(" · ")}</dd>
              <dt>Looks at</dt>
              <dd>{prompt.metrics.join(" · ")}</dd>
              <dt>The prompt</dt>
              <dd>
                <pre className="review-prompt-text">{prompt.instructions}</pre>
              </dd>
            </dl>
          </details>
        )}
        {choices.length ? (
          <div className="filters review-bar">
            <label className="ask-model">
              <span>Review with</span>
              <select
                value={value}
                disabled={busy}
                onChange={(event) => {
                  const [backend, ...rest] = event.target.value.split("\n");
                  setPicked({ backend, model: rest.join("\n") });
                }}
              >
                {choices.map((group) => (
                  <optgroup key={group.backend} label={`${group.backend} (${providerLabel(group.type)})`}>
                    {group.models.map((model) => (
                      <option key={model} value={`${group.backend}\n${model}`}>
                        {model}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <label className="review-question">
              <span>Anything else to ask (optional)</span>
              <input value={question} maxLength={500} disabled={busy} onChange={(event) => setQuestion(event.target.value)} placeholder="Would a smaller model have done?" />
            </label>
            {busy ? (
              <button type="button" onClick={() => stopReviewRun(id)}>
                Stop
              </button>
            ) : (
              <button type="button" className="primary" onClick={start}>
                {reviews.length || phase ? "Review again" : "Review this session"}
              </button>
            )}
          </div>
        ) : (
          <p className="subtle">
            {models.isLoading ? "Looking for models…" : "No provider you can use takes a review right now: it needs one with a model that speaks OpenAI Chat."}{" "}
            {!models.isLoading && <Link to="/providers">Providers</Link>}
          </p>
        )}
        {phase && (busy || !shown) && <LiveReview reviewed={id} />}
        {reviews.length > 0 && (
          <>
            <h3 className="review-earlier">Earlier reviews</h3>
            <ul className="review-runs">
              {reviews.map((item) => {
                const running = busy && item.session_id === runId;
                return (
                  <li key={item.session_id}>
                    <button
                      type="button"
                      className="link"
                      aria-expanded={running ? !shown : shown === item.session_id}
                      onClick={() => setShown(running || shown === item.session_id ? null : item.session_id)}
                    >
                      {formatDate(item.started_at)}
                    </button>
                    <span>{item.prompt}</span>
                    <span>{item.model}</span>
                    <span className="subtle">
                      on {item.backend}
                      {item.by ? `, by ${item.by}` : ""}
                    </span>
                    {running ? <span className="pill live">Running</span> : !item.ended_at && <span className="pill">Not finished</span>}
                  </li>
                );
              })}
            </ul>
          </>
        )}
        {shown && (
          <article className="review-answer">
            <header>
              <strong>{past.data ? [past.data.prompt, past.data.model].filter(Boolean).join(", ") : "Review"}</strong>
              <Link className="subtle" to={`/sessions/${shown}?view=chat`}>
                Open as a session
              </Link>
            </header>
            {past.isError ? (
              <p className="bad">{past.error.message}</p>
            ) : !past.data ? (
              <p className="subtle">Reading the review…</p>
            ) : past.data.text ? (
              <Prose text={spaced(past.data.text)} />
            ) : (
              <p className="subtle">
                This review has no saved answer: it was stopped before the model began its answer, or its payloads weren't kept. Open it as a session to see
                what the model got as far as.
              </p>
            )}
          </article>
        )}
      </section>
    </div>
  );
}

const STEPS = ["Sent", "Thinking", "Writing", "Finished"] as const;

/**
 * The review this page started, as it goes: where it stands, how long it has taken, the model's thinking
 * while there's no answer yet, then the answer. It reads the run from reviewRuns.ts, so it shows the same
 * run after the tab was left and opened again.
 */
function LiveReview({ reviewed }: { reviewed: string }) {
  const run = useReviewRun(reviewed);
  const busy = reviewBusy(run);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [busy]);
  // The thinking is open while it's all there is, and folds away once the answer starts, unless someone chose.
  const [fold, setFold] = useState<boolean | null>(null);
  const pane = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const element = pane.current;
    if (element && busy) element.scrollTop = element.scrollHeight;
  }, [run?.reasoning, busy]);
  if (!run) return null;

  const seconds = Math.max(0, Math.round(((run.endedAt ?? now) - run.startedAt) / 1000));
  const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  const thought = Math.round(run.reasoning.length / 4);
  // How far it got: 0 sent, 1 thinking, 2 writing, 3 finished.
  const reached = run.phase === "done" ? 3 : run.text ? 2 : run.reasoning ? 1 : 0;
  const showThinking = fold ?? (run.phase === "thinking" || (!run.text && !busy));
  const status = {
    starting: "Opening the review's session…",
    waiting: `Sent to ${run.model}. Waiting for it to read the session's data…`,
    thinking: `${run.model} is thinking: about ${formatNumber(thought)} tokens so far. The answer comes after, which can take a minute or more.`,
    writing: "Writing the answer…",
    done: `Finished in ${clock}. It's kept under Earlier reviews.`,
    stopped: `Stopped after ${clock}.${run.text ? "" : " The model hadn't begun its answer."}`,
    failed: `Failed after ${clock}.`,
  }[run.phase];

  return (
    <article className={`review-answer review-run phase-${run.phase}`} aria-busy={busy}>
      <header>
        <strong>
          {run.title}, {run.model}
        </strong>
        {run.id && (
          <Link className="subtle" to={`/sessions/${run.id}?view=chat`} title={busy ? "The review keeps running while you look" : undefined}>
            Open as a session
          </Link>
        )}
      </header>
      <ol className="review-steps" aria-hidden="true">
        {STEPS.map((name, index) => {
          const skipped = index === 1 && reached > 1 && !run.reasoning;
          const state = skipped ? "skipped" : index < reached || run.phase === "done" ? "done" : index === reached && busy && run.phase !== "starting" ? "now" : "";
          return (
            <li key={name} className={state}>
              {name}
            </li>
          );
        })}
      </ol>
      <p className="review-status" role="status">
        {busy && (
          <span className="chat-typing" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        )}
        <span>{status}</span>
        {busy && <span className="review-clock">{clock}</span>}
      </p>
      {run.failed && <p className="bad">{run.failed}</p>}
      {run.reasoning && (
        <div className="review-thinking">
          <button type="button" className="link" aria-expanded={showThinking} onClick={() => setFold(!showThinking)}>
            {showThinking ? "Hide" : "Show"} the model's thinking (about {formatNumber(thought)} tokens)
          </button>
          {showThinking && <pre ref={pane}>{run.reasoning}</pre>}
        </div>
      )}
      {run.text ? <Prose text={spaced(run.text)} /> : run.phase === "done" && <p className="subtle">The model answered nothing.</p>}
      {busy && <p className="subtle review-note">It keeps running if you leave this tab or open it as a session. Closing or reloading the console stops it.</p>}
    </article>
  );
}

/** The numbers behind the checks, as the server measured them: where the context went and what the agent did. */
function Evidence({ review }: { review: Review }) {
  const { timeline, context, prompt, tools, conversation, traffic } = review;
  const points = timeline.points;
  const top = Math.max(...points.map((point) => point.tokens), traffic.context_window ?? 0, 1);
  // The context's size call by call, as a line across a 600 by 120 box.
  const line = points.map((point, index) => `${(points.length > 1 ? (index / (points.length - 1)) * 600 : 300).toFixed(1)},${(118 - (point.tokens / top) * 114).toFixed(1)}`).join(" ");
  return (
    <section className="review-part" id="review-measured">
      <h2>What was measured</h2>
      <div className="review-evidence">
        {points.length > 1 && (
          <figure className="review-figure">
            <figcaption>
              The context, call by call: {formatCompact(points[0].tokens)} tokens at the first of {formatNumber(timeline.calls)} calls,{" "}
              {formatCompact(traffic.peak_context_tokens)} at the largest
              {timeline.growth_per_call > 0 && `, about ${formatCompact(timeline.growth_per_call)} more each call`}.
            </figcaption>
            <svg viewBox="0 0 600 120" preserveAspectRatio="none" role="img" aria-label="The context's size over the session's calls">
              <polyline points={`0,120 ${line} 600,120`} className="review-area" />
              <polyline points={line} className="review-line" />
            </svg>
          </figure>
        )}
        {context && context.length > 0 && (
          <figure className="review-figure">
            <figcaption>What the main chat's context holds (estimated).</figcaption>
            <div className="review-bar-parts" role="img" aria-label={context.map((item) => `${item.part} ${Math.round(item.share * 100)}%`).join(", ")}>
              {context.map((item, index) => (
                <span key={item.part} style={{ flexGrow: item.share, background: `var(--series-${(index % 8) + 1})` }} title={`${item.part}: ${formatNumber(item.tokens)} tokens`} />
              ))}
            </div>
            <ul className="review-legend">
              {context.map((item, index) => (
                <li key={item.part}>
                  <i style={{ background: `var(--series-${(index % 8) + 1})` }} />
                  {item.part} <span className="subtle">{formatCompact(item.tokens)} · {Math.round(item.share * 100)}%</span>
                </li>
              ))}
            </ul>
          </figure>
        )}
        {traffic.tiers.length > 0 && (
          <figure className="review-figure">
            <figcaption>Model calls by tier, from each model's list price for input.</figcaption>
            <div className="review-bar-parts" role="img" aria-label={traffic.tiers.map((item) => `${TIER[item.tier].label} ${Math.round(item.share * 100)}%`).join(", ")}>
              {traffic.tiers.map((item) => (
                <span key={item.tier} style={{ flexGrow: item.share, background: TIER[item.tier].color }} title={`${TIER[item.tier].label}: ${formatNumber(item.calls)} calls`} />
              ))}
            </div>
            <ul className="review-legend">
              {traffic.tiers.map((item) => (
                <li key={item.tier}>
                  <i style={{ background: TIER[item.tier].color }} />
                  {TIER[item.tier].label}{" "}
                  <span className="subtle">
                    {formatNumber(item.calls)} calls · {Math.round(item.share * 100)}%{item.usd > 0 ? ` · ${formatUsd(item.usd)}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </figure>
        )}
      </div>
      {traffic.models.length > 0 && (
        <Table
          title={`Models: ${formatNumber(traffic.models.length)}`}
          head={["Model", "Tier", "Calls", "Tokens read", "Tokens written", "Estimated cost"]}
          rows={traffic.models.map((item) => [
            item.model ?? "unknown",
            TIER[item.tier].label,
            formatNumber(item.calls),
            formatNumber(item.input_tokens),
            formatNumber(item.output_tokens),
            item.tier === "local" ? "none" : item.tier === "unpriced" ? "no price" : formatUsd(item.usd),
          ])}
          note="A tier comes from the model's list price for input in the server's price table: frontier is the most expensive, then mid-range and small. Local is a machine's own model."
        />
      )}
      {prompt && prompt.sections.length > 1 && (
        <Table
          title={`The system prompt's sections (${formatCompact(prompt.system_tokens)} tokens)`}
          head={["Section", "Tokens"]}
          rows={prompt.sections.map((item) => [item.title, formatNumber(item.tokens)])}
        />
      )}
      {prompt && prompt.tools_defined > 0 && (
        <Table
          title={`Tool definitions: ${formatNumber(prompt.tools_defined)}, ${formatCompact(prompt.tool_definition_tokens)} tokens, ${formatNumber(prompt.tools_unused.length)} never called`}
          head={["Largest", "Tokens"]}
          rows={prompt.largest_tools.map((item) => [item.name, formatNumber(item.tokens)])}
          note={prompt.mcp_servers.length ? `MCP servers: ${prompt.mcp_servers.map((item) => `${item.name} (${item.tools} tools, ${item.calls} calls)`).join(", ")}` : undefined}
        />
      )}
      {tools && tools.by_name.length > 0 && (
        <Table
          title={`Tool calls: ${formatNumber(tools.calls)}`}
          head={["Tool", "Kind", "Calls", "Failed", "Result tokens"]}
          rows={tools.by_name.map((item) => [item.name, item.category, formatNumber(item.calls), formatNumber(item.errors), formatNumber(Math.floor(item.result_chars / 4))])}
        />
      )}
      {tools && tools.commands.length > 0 && (
        <Table
          title="Shell commands"
          head={["Command", "Times", "Failed", "Result tokens"]}
          rows={tools.commands.map((item) => [item.example, formatNumber(item.calls), formatNumber(item.errors), formatNumber(item.result_tokens)])}
        />
      )}
      {tools && tools.error_samples.length > 0 && (
        <Table
          title="Failed tool calls"
          head={["Tool", "Given", "Times", "Error"]}
          rows={tools.error_samples.map((item) => [item.name, item.target, formatNumber(item.times), item.error])}
        />
      )}
      {tools && (tools.edited_files.length > 0 || tools.reread_files.length > 0) && (
        <Table
          title="Files"
          head={["File", "Edits", "Reads of 3 or more"]}
          rows={[...new Set([...tools.edited_files.map((item) => item.file), ...tools.reread_files.map((item) => item.file)])].map((file) => [
            file,
            formatNumber(tools.edited_files.find((item) => item.file === file)?.edits ?? 0),
            formatNumber(tools.reread_files.find((item) => item.file === file)?.reads ?? 0),
          ])}
        />
      )}
      {conversation && conversation.steps.length > 0 && (
        <Table
          title={`The main chat, step by step${conversation.step_count > conversation.steps.length ? ` (the first ${conversation.steps.length} of ${conversation.step_count})` : ""}`}
          head={["Typed", "Tool calls", "Of which", "Result tokens"]}
          rows={conversation.steps.map((step) => [
            step.typed,
            formatNumber(step.tool_calls),
            Object.entries(step.by_kind).map(([kind, count]) => `${kind} ${count}`).join(", "),
            formatNumber(step.result_tokens),
          ])}
        />
      )}
      {conversation && conversation.threads.length > 0 && (
        <Table
          title="Sub-agents and side conversations"
          head={["Asked", "Kind", "Model", "Model calls", "Tool calls"]}
          rows={conversation.threads.map((item) => [item.title, item.kind, item.model, formatNumber(item.calls), formatNumber(item.tool_calls)])}
        />
      )}
    </section>
  );
}

function Table({ title, head, rows, note }: { title: string; head: string[]; rows: string[][]; note?: string }) {
  return (
    <details className="review-table">
      <summary>{title}</summary>
      {note && <p className="subtle">{note}</p>}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {head.map((name) => (
                <th key={name}>{name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={index}>
                {row.map((cell, position) => (
                  <td key={position}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}
