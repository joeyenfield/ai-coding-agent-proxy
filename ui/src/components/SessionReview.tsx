import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, askModel, preferredModel, providerModels, type ReviewFinding, type Session, type SessionReview as Review } from "../api";
import { formatCompact, formatDate, formatNumber } from "../format";
import { useModels, useToast } from "../hooks";
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

/**
 * The Review tab of a session: what the server's checks found in it (measured there, never here), the
 * numbers behind them, and an AI review by a model you pick. The AI review is a session of its own, sent
 * through the server like any model call: the server writes its messages, this page only carries them.
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
  const [run, setRun] = useState<{ id: string; title: string; model: string; text: string; busy: boolean; failed?: string } | null>(null);
  const [shown, setShown] = useState<string | null>(null);
  const stop = useRef<AbortController | null>(null);
  const running = useRef<string | null>(null);
  // Leaving the tab stops a review under way and ends its session.
  useEffect(
    () => () => {
      stop.current?.abort();
      if (running.current) api.endSession(running.current, { keepalive: true }).catch(() => undefined);
    },
    [],
  );
  const past = useQuery({
    queryKey: ["session-review-text", id, shown],
    queryFn: () => api.readReview(id, shown!),
    enabled: Boolean(shown),
    retry: false,
  });

  const start = async () => {
    if (!choice || !prompt || run?.busy) return;
    const controller = new AbortController();
    stop.current = controller;
    setShown(null);
    setRun({ id: "", title: prompt.title, model: choice.model, text: "", busy: true });
    let opened: string | null = null;
    try {
      const made = await api.startReview(id, { ...choice, prompt: prompt.id, question: question.trim() || undefined });
      opened = running.current = made.session.session_id;
      setRun({ id: opened, title: prompt.title, model: made.model, text: "", busy: true });
      await askModel(
        opened,
        { model: made.model, messages: made.messages },
        (piece) => piece.content && setRun((current) => (current ? { ...current, text: current.text + piece.content } : current)),
        controller.signal,
      );
      setRun((current) => (current ? { ...current, busy: false } : current));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setRun((current) => (current ? { ...current, busy: false, failed: controller.signal.aborted ? undefined : message } : current));
      if (!controller.signal.aborted) notify(message);
    } finally {
      stop.current = null;
      running.current = null;
      if (opened) await api.endSession(opened).catch(() => undefined);
      client.invalidateQueries({ queryKey: ["session-review", id] });
      client.invalidateQueries({ queryKey: ["sessions"] });
    }
  };
  const stopRun = () => {
    stop.current?.abort();
    if (running.current) api.stopReplies(running.current).catch(() => undefined);
  };

  if (review.isError) return <p className="bad">{review.error.message}</p>;
  if (!review.data) return <p className="empty">Checking the session…</p>;
  const { traffic, prompt: sent, tools, findings, reviews, prompts, score, grade } = review.data;
  if (!traffic.model_calls) return <p className="empty">No model calls yet. A review needs some: come back once the agent has talked to its model.</p>;
  const value = choice ? `${choice.backend}\n${choice.model}` : "";

  return (
    <div className="review">
      <dl className="stat-cards" aria-label="What the review measured">
        {score != null && (
          <div>
            <dt>Score</dt>
            <dd title="100, less 18 for each high finding, 9 for each medium and 3 for each low">
              {score}
              <small> {grade}</small>
            </dd>
          </div>
        )}
        <div>
          <dt>Model calls</dt>
          <dd>{formatNumber(traffic.model_calls)}</dd>
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
      </dl>

      <section className="review-part">
        <div className="section-head">
          <h2>Checks</h2>
          <button type="button" className="small" onClick={() => review.refetch()} disabled={review.isFetching}>
            {review.isFetching ? "Checking…" : "Check again"}
          </button>
        </div>
        {findings.length ? (
          <ul className="review-findings">
            {findings.map((finding) => (
              <li key={finding.id} className={`review-finding severity-${finding.severity}`}>
                <div className="review-finding-head">
                  <span className={SEVERITY[finding.severity].pill}>{SEVERITY[finding.severity].label}</span>
                  <strong>{finding.title}</strong>
                  <span className="subtle">
                    {finding.impact_tokens ? `about ${formatCompact(finding.impact_tokens)} tokens · ` : ""}
                    {AREA[finding.area]}
                  </span>
                </div>
                <p>{finding.detail}</p>
                <p className="review-suggestion">
                  <strong>Try:</strong> {finding.suggestion}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty">Nothing stood out. The checks look at the context's size, how tools were used, how the session went, failures and caching.</p>
        )}
      </section>

      <Evidence review={review.data} />

      <section className="review-part">
        <h2>AI review</h2>
        <p className="subtle">
          A model reads what was measured and suggests what to change. Choose what it should look at: each review has its own
          question and is sent only the data it needs, never the whole transcript. It runs as a private session of your own.
        </p>
        <div className="chip-bar" role="group" aria-label="What to review">
          {prompts.map((item) => (
            <button key={item.id} type="button" aria-pressed={item.id === prompt?.id} disabled={run?.busy} onClick={() => setFocus(item.id)}>
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
                disabled={run?.busy}
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
              <input value={question} maxLength={500} disabled={run?.busy} onChange={(event) => setQuestion(event.target.value)} placeholder="Would a smaller model have done?" />
            </label>
            {run?.busy ? (
              <button type="button" onClick={stopRun}>
                Stop
              </button>
            ) : (
              <button type="button" className="primary" onClick={start}>
                {reviews.length || run ? "Review again" : "Review this session"}
              </button>
            )}
          </div>
        ) : (
          <p className="subtle">
            {models.isLoading ? "Looking for models…" : "No provider you can use takes a review right now: it needs one with a model that speaks OpenAI Chat."}{" "}
            {!models.isLoading && <Link to="/providers">Providers</Link>}
          </p>
        )}
        {run && !shown && (
          <article className="review-answer">
            <header>
              <strong>
                {run.title}, {run.model}
              </strong>
              {run.busy && (
                <span className="chat-typing" aria-label="writing">
                  <i />
                  <i />
                  <i />
                </span>
              )}
              {run.id && (
                <Link className="subtle" to={`/sessions/${run.id}`}>
                  Open as a session
                </Link>
              )}
            </header>
            {run.text ? <Prose text={spaced(run.text)} /> : !run.busy && !run.failed && <p className="subtle">The model answered nothing.</p>}
            {run.failed && <p className="bad">{run.failed}</p>}
          </article>
        )}
        {reviews.length > 0 && (
          <>
            <h3 className="review-earlier">Earlier reviews</h3>
            <ul className="review-runs">
              {reviews.map((item) => (
                <li key={item.session_id}>
                  <button type="button" className="link" aria-expanded={shown === item.session_id} onClick={() => setShown(shown === item.session_id ? null : item.session_id)}>
                    {formatDate(item.started_at)}
                  </button>
                  <span>{item.prompt}</span>
                  <span>{item.model}</span>
                  <span className="subtle">
                    on {item.backend}
                    {item.by ? `, by ${item.by}` : ""}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
        {shown && (
          <article className="review-answer">
            <header>
              <strong>{past.data ? [past.data.prompt, past.data.model].filter(Boolean).join(", ") : "Review"}</strong>
              <Link className="subtle" to={`/sessions/${shown}`}>
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
              <p className="subtle">This review has no saved answer: it was stopped, or its payloads weren't kept.</p>
            )}
          </article>
        )}
      </section>
    </div>
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
    <section className="review-part">
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
      </div>
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
