import { createContext, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { api, tokens, type ChatBlock, type ChatThread, type ChatTurn, type RequestRecord, type Session, type SessionChat as ChatPage } from "../api";
import { formatDuration, formatNumber, formatRelative } from "../format";
import { useToast } from "../hooks";
import type { LiveRequest } from "../live";
import { requestPath } from "./RequestList";

const PAGE = 60;
// Older messages load once the top of the pane is this close.
const LOAD_MARGIN = "600px";

/** Where a block lives, so a preview can ask the server for its whole turn. */
interface Place {
  turn: number;
  position: number;
}

const ChatSource = createContext<{ sessionId: string; thread: string }>({ sessionId: "", thread: "" });

/**
 * The session read back as a chat, the way the agent's own window shows it: built by the server
 * from the newest captured call of each conversation, each reply linked to the request that wrote it.
 * It opens on the newest messages and loads older pages as you scroll up; long tool output comes
 * as a preview, and the whole of it only when it's opened.
 */
export function SessionChat({ session, records, streaming }: { session: Session; records: RequestRecord[]; streaming: LiveRequest[] }) {
  const [thread, setThread] = useState<string | null>(null);
  const [reminders, setReminders] = useState(false);
  const client = useQueryClient();
  const key = ["chat", session.session_id, thread];
  const chat = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam }) => api.chat(session.session_id, { thread, before: pageParam, limit: PAGE }),
    initialPageParam: undefined as number | undefined,
    // Pages run newest first; each next one ends where the last began.
    getNextPageParam: (last) => (last.start > 0 ? last.start : undefined),
    staleTime: Infinity,
  });

  // New requests can add turns at the end: fetch just the newest page again and put it in front,
  // rather than refetching every page scrolled back through.
  const loaded = Boolean(chat.data);
  useEffect(() => {
    if (!loaded) return;
    let cancelled = false;
    api.chat(session.session_id, { thread, limit: PAGE }).then(
      (fresh) => {
        if (cancelled) return;
        client.setQueryData<InfiniteData<ChatPage, number | undefined>>(key, (old) => {
          if (!old) return old;
          const newest = old.pages[0];
          // Too many new turns to join up, or the history was rewritten: start again from the newest.
          if (fresh.start > newest.start + newest.turns.length || fresh.total < newest.total || fresh.thread !== newest.thread)
            return { pages: [fresh], pageParams: [undefined] };
          // Fold it into the newest page, so a long live session doesn't pile up pages.
          const merged = { ...fresh, start: newest.start, turns: [...newest.turns.filter((turn) => turn.index < fresh.start), ...fresh.turns] };
          return { pages: [merged, ...old.pages.slice(1)], pageParams: old.pageParams };
        });
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
    // Only a change in the session's request count means there may be more to read.
  }, [session.request_count]);

  const pages = chat.data?.pages;
  const first = pages?.[0];
  const turns = useMemo(() => {
    const byIndex = new Map<number, ChatTurn>();
    // Later pages are older; the newest copy of a turn wins.
    for (const page of [...(pages ?? [])].reverse()) for (const turn of page.turns) byIndex.set(turn.index, turn);
    const total = pages?.[0]?.total ?? 0;
    return [...byIndex.values()].filter((turn) => turn.index < total).sort((a, b) => a.index - b.index);
  }, [pages]);
  const byId = useMemo(() => new Map(records.map((record) => [record.request_id, record])), [records]);
  const results = useMemo(() => {
    const map = new Map<string, { block: ToolResult } & Place>();
    for (const turn of turns)
      turn.blocks.forEach((block, position) => {
        if (block.type === "tool_result" && block.tool_use_id) map.set(block.tool_use_id, { block, turn: turn.index, position });
      });
    return map;
  }, [turns]);
  const groups = useMemo(() => {
    const paired = new Set(turns.flatMap((turn) => turn.blocks.flatMap((block) => (block.type === "tool_use" && results.has(block.id) ? [block.id] : []))));
    return groupTurns(turns, paired, reminders);
  }, [turns, results, reminders]);

  const current = first?.threads.find((item) => item.id === first.thread);
  const live = current?.main && !session.ended_at ? streaming : [];
  const pane = useStickyScroll(first ? `${session.session_id}:${first.thread}` : "", turns[0]?.index ?? 0, turns.length + live.length);
  const top = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = chat;
  useEffect(() => {
    const target = top.current;
    if (!target || !hasNextPage) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !isFetchingNextPage) fetchNextPage();
      },
      { root: pane.current, rootMargin: `${LOAD_MARGIN} 0px 0px 0px` },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, pane, turns.length]);

  if (chat.isPending) return <p className="empty">Reading the captured requests…</p>;
  if (chat.isError) return <p className="notice bad">{chat.error.message}</p>;
  if (!first || !first.traced_calls) return <NoTraces session={session} calls={first?.model_calls ?? 0} />;

  return (
    <div className="chat">
      <div className="filters">
        <ThreadPicker threads={first.threads} value={first.thread} onChange={setThread} />
        <label className="check">
          <input type="checkbox" checked={reminders} onChange={(event) => setReminders(event.target.checked)} />
          Show system reminders
        </label>
        {first.traced_calls < first.model_calls && (
          <span className="subtle">
            {formatNumber(first.traced_calls)} of {formatNumber(first.model_calls)} model calls were captured
          </span>
        )}
      </div>
      {current && (
        <p className="subtle chat-about">
          {current.model || "Unknown model"}, {formatNumber(current.requests.length)} {current.requests.length === 1 ? "call" : "calls"},{" "}
          {formatNumber(current.messages)} messages, last {formatRelative(current.updated)}
        </p>
      )}
      <ChatSource.Provider value={{ sessionId: session.session_id, thread: first.thread ?? "" }}>
        <div className="chat-scroll" ref={pane}>
          <div ref={top} className="chat-top subtle">
            {hasNextPage ? (
              isFetchingNextPage ? "Loading earlier messages…" : `${formatNumber(turns[0]?.index ?? 0)} earlier messages`
            ) : (
              "Start of the conversation"
            )}
          </div>
          <ol className="chat-list">
            {groups.map((group) => (
              <Group key={group.first} group={group} results={results} sessionId={session.session_id} records={byId} />
            ))}
            {live.map((entry) => (
              <li key={entry.key} className="chat-turn chat-as-assistant">
                <div className="chat-role">
                  Assistant <span className="pill live">writing</span>
                </div>
                {entry.reasoning && <Fold label="Thinking" className="chat-thinking" text={entry.reasoning} />}
                {entry.content && <Prose text={entry.content} />}
                {entry.tool && <pre className="chat-code wrap">{entry.tool}</pre>}
                <Link className="chat-meta subtle" to={requestPath(entry)}>
                  Request #{entry.request_id}, streaming
                </Link>
              </li>
            ))}
          </ol>
        </div>
      </ChatSource.Provider>
    </div>
  );
}

/**
 * The chat pane's scrolling: it opens at the bottom, stays there as new messages arrive while you're
 * at the bottom, and keeps your place when older messages are put in above.
 */
function useStickyScroll(view: string, oldest: number, count: number) {
  const pane = useRef<HTMLDivElement>(null);
  const state = useRef({ view: "", oldest: 0, fromBottom: 0, atBottom: true });
  useEffect(() => {
    const element = pane.current;
    if (!element) return;
    const onScroll = () => {
      state.current.fromBottom = element.scrollHeight - element.scrollTop;
      state.current.atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 40;
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    // Cards change height after they're put in (fonts, opened folds); keep the bottom in view if that's where you are.
    const resize = new ResizeObserver(() => {
      if (state.current.atBottom) element.scrollTop = element.scrollHeight;
    });
    if (element.firstElementChild) for (const child of element.children) resize.observe(child);
    return () => {
      element.removeEventListener("scroll", onScroll);
      resize.disconnect();
    };
  }, [view]);
  useLayoutEffect(() => {
    const element = pane.current;
    if (!element || !view) return;
    const saved = state.current;
    if (saved.view !== view || saved.atBottom) element.scrollTop = element.scrollHeight;
    else if (oldest < saved.oldest) element.scrollTop = element.scrollHeight - saved.fromBottom;
    saved.view = view;
    saved.oldest = oldest;
    saved.fromBottom = element.scrollHeight - element.scrollTop;
  }, [view, oldest, count]);
  return pane;
}

function ThreadPicker({ threads, value, onChange }: { threads: ChatThread[]; value: string | null; onChange: (id: string) => void }) {
  if (threads.length < 2) return null;
  const label = (thread: ChatThread) =>
    `${thread.main ? "Main chat: " : ""}${thread.title || "Untitled"} (${thread.requests.length} ${thread.requests.length === 1 ? "call" : "calls"})`;
  const chats = threads.filter((thread) => thread.main || thread.requests.length > 1);
  const single = threads.filter((thread) => !thread.main && thread.requests.length === 1);
  return (
    <select className="chat-picker" aria-label="Conversation" value={value ?? ""} onChange={(event) => onChange(event.target.value)}>
      <optgroup label="Conversations">
        {chats.map((thread) => (
          <option key={thread.id} value={thread.id}>
            {label(thread)}
          </option>
        ))}
      </optgroup>
      {single.length > 0 && (
        <optgroup label="One-off calls">
          {single.map((thread) => (
            <option key={thread.id} value={thread.id}>
              #{thread.id} {thread.title || thread.model}
            </option>
          ))}
        </optgroup>
      )}
    </select>
  );
}

type ToolResult = Extract<ChatBlock, { type: "tool_result" }>;
type Results = Map<string, { block: ToolResult } & Place>;

/** Turns as they're shown: one card per message, with an assistant's steps between tool calls in one card. */
interface TurnGroup {
  /** The index of its first turn, which keeps its key steady as pages load around it. */
  first: number;
  role: ChatTurn["role"];
  preamble?: boolean;
  steps: { blocks: ({ block: ChatBlock } & Place)[]; request_id?: string }[];
}

function groupTurns(turns: ChatTurn[], paired: Set<string>, reminders: boolean): TurnGroup[] {
  const groups: TurnGroup[] = [];
  for (const turn of turns) {
    if (turn.role === "system" && !turn.preamble && !reminders) continue;
    const blocks = turn.blocks
      .map((block, position) => ({ block, turn: turn.index, position }))
      .filter(
        ({ block }) =>
          !(block.type === "text" && block.reminder && !reminders) &&
          // Claude Code sends its thinking back without the text; there's nothing to show.
          !(block.type === "thinking" && !block.text) &&
          // Tool results shown inside their call aren't repeated on their own.
          !(block.type === "tool_result" && paired.has(block.tool_use_id)),
      );
    if (!blocks.length) continue;
    const last = groups[groups.length - 1];
    if (last && last.role === "assistant" && turn.role === "assistant") last.steps.push({ blocks, request_id: turn.request_id });
    else groups.push({ first: turn.index, role: turn.role, preamble: turn.preamble, steps: [{ blocks, request_id: turn.request_id }] });
  }
  return groups;
}

const ROLE_LABELS: Record<ChatTurn["role"], string> = { system: "System", user: "You", assistant: "Assistant", tool: "Tool output" };

function Group({ group, results, sessionId, records }: { group: TurnGroup; results: Results; sessionId: string; records: Map<string, RequestRecord> }) {
  if (group.role === "system") {
    const blocks = group.steps.flatMap((step) => step.blocks);
    const text = blocks.map(({ block }) => ("text" in block ? block.text : "")).join("\n\n");
    const label = group.preamble ? "System prompt" : "System message";
    // A long system prompt is cut in the page; opening it fetches the whole turn.
    return (
      <li className="chat-turn chat-as-system">
        {blocks.length === 1 ? <Fold label={label} text={text} mono place={blocks[0]} preview={blocks[0].block.preview} /> : <Fold label={label} text={text} mono />}
      </li>
    );
  }
  return (
    <li className={`chat-turn chat-as-${group.role}`}>
      <div className="chat-role">{ROLE_LABELS[group.role]}</div>
      {group.steps.map((step, index) => (
        <div key={index} className="chat-step">
          {step.blocks.map((item) => (
            <Block key={`${item.turn}-${item.position}`} item={item} results={results} />
          ))}
          {step.request_id && <RequestMeta id={step.request_id} record={records.get(step.request_id)} sessionId={sessionId} />}
        </div>
      ))}
    </li>
  );
}

function Block({ item, results }: { item: { block: ChatBlock } & Place; results: Results }) {
  const { block } = item;
  switch (block.type) {
    case "text":
      return block.reminder ? (
        <Fold label="System reminder" className="chat-reminder" text={block.text} mono place={item} preview={block.preview} />
      ) : (
        <Prose text={block.text} truncated={block.truncated} />
      );
    case "thinking":
      return <Fold label="Thinking" className="chat-thinking" text={block.text} place={item} preview={block.preview} />;
    case "tool_use":
      return <ToolCall block={block} place={item} result={results.get(block.id)} />;
    case "tool_result":
      return (
        <Fold
          label={block.is_error ? "Error" : "Result"}
          className={block.is_error ? "chat-error" : ""}
          text={block.text}
          mono
          truncated={block.truncated}
          place={item}
          preview={block.preview}
        />
      );
    case "image":
      return <p className="subtle chat-note">[image]</p>;
    case "error":
      return <pre className="chat-code wrap chat-error-text">{block.text}</pre>;
    default:
      return <Fold label="Other content" text={block.text} mono truncated={block.truncated} place={item} preview={block.preview} />;
  }
}

/** The whole of a block a page only had the start of, fetched when it's opened. */
function useFullBlock(place: Place | undefined, wanted: boolean): ChatBlock | undefined {
  const { sessionId, thread } = useContext(ChatSource);
  const turn = useQuery({
    queryKey: ["chat-turn", sessionId, thread, place?.turn],
    queryFn: () => api.chatTurn(sessionId, thread, place!.turn),
    enabled: wanted && Boolean(place && thread),
    staleTime: Infinity,
  });
  return place ? turn.data?.blocks[place.position] : undefined;
}

function ToolCall({ block, place, result }: { block: Extract<ChatBlock, { type: "tool_use" }>; place: Place; result?: { block: ToolResult } & Place }) {
  const [open, setOpen] = useState(false);
  const fullCall = useFullBlock(place, open && Boolean(block.preview));
  const fullResult = useFullBlock(result, open && Boolean(result?.block.preview));
  const call = fullCall?.type === "tool_use" ? fullCall : block;
  const output = fullResult?.type === "tool_result" ? fullResult : result?.block;
  const input = typeof call.input === "string" ? call.input : JSON.stringify(call.input, null, 2);
  const loading = (block.preview && !fullCall) || (result?.block.preview && !fullResult);
  return (
    <details className={`chat-tool ${output?.is_error ? "is-error" : ""}`} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <span className="chat-tool-name">{block.name}</span>
        <span className="mono subtle chat-tool-arg">{toolSummary(block.input)}</span>
        {output?.is_error && <span className="pill bad">error</span>}
        {!output && <span className="subtle">no result</span>}
      </summary>
      {open && (
        <div className="chat-tool-body">
          {loading && <p className="subtle chat-note">Loading the full text…</p>}
          <div className="chat-tool-label">Input</div>
          <pre className="chat-code wrap">{input}</pre>
          {output && (
            <>
              <div className="chat-tool-label">{output.is_error ? "Error" : "Result"}</div>
              <pre className="chat-code wrap">{output.text || "(empty)"}</pre>
              {output.truncated && <Truncated length={output.truncated} />}
            </>
          )}
        </div>
      )}
    </details>
  );
}

/** A collapsed section whose text is only put on the page once it's opened, and fetched whole then if the page had a preview. */
function Fold({
  label,
  text,
  className = "",
  mono = false,
  truncated,
  place,
  preview,
}: {
  label: ReactNode;
  text: string;
  className?: string;
  mono?: boolean;
  truncated?: number;
  place?: Place;
  preview?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const full = useFullBlock(place, open && Boolean(preview));
  const shown = full && "text" in full ? full.text : text;
  const cut = full && "truncated" in full ? full.truncated : truncated;
  return (
    <details className={`chat-fold ${className}`} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        {label}
        {!open && <span className="subtle chat-fold-peek">{peek(text)}</span>}
      </summary>
      {open && preview && !full && <p className="subtle chat-note">Loading the full text…</p>}
      {open && (mono ? <pre className="chat-code wrap">{shown}</pre> : <Prose text={shown} />)}
      {open && cut && <Truncated length={cut} />}
    </details>
  );
}

/** Markdown as agents write it, kept simple: code blocks, headings, lists, bold and inline code. */
function Prose({ text, truncated }: { text: string; truncated?: number }) {
  const parts = text.split(/^```[^\n]*\n?/m);
  return (
    <div className="chat-prose">
      {parts.map((part, index) =>
        index % 2 ? (
          <pre key={index} className="chat-code">
            {part.replace(/\n$/, "")}
          </pre>
        ) : (
          paragraphs(part).map((lines, position) => <Paragraph key={`${index}-${position}`} lines={lines} />)
        ),
      )}
      {truncated && <Truncated length={truncated} />}
    </div>
  );
}

const paragraphs = (text: string) =>
  text
    .split(/\n\s*\n/)
    .map((block) => block.split("\n"))
    .filter((lines) => lines.some((line) => line.trim()));

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

function Paragraph({ lines }: { lines: string[] }) {
  const heading = lines.length === 1 ? /^(#{1,6})\s+(.*)$/.exec(lines[0]) : null;
  if (heading) return <p className="chat-heading">{inline(heading[2])}</p>;
  if (lines.every((line) => LIST_ITEM.test(line) || /^\s{2,}\S/.test(line))) {
    return (
      <ul className="chat-bullets">
        {lines.map((line, index) => {
          const item = LIST_ITEM.exec(line);
          return (
            <li key={index} style={{ marginLeft: `${Math.min(3, Math.floor((item?.[1] ?? line.match(/^\s*/)![0]).length / 2)) * 16}px` }}>
              {item && /\d/.test(item[2]) && <span className="chat-number">{item[2]} </span>}
              {inline(item ? item[3] : line.trim())}
            </li>
          );
        })}
      </ul>
    );
  }
  return <p>{lines.map((line, index) => (index ? ["\n", ...inline(line)] : inline(line)))}</p>;
}

/** **bold** and `code` inside a line. */
function inline(text: string): ReactNode[] {
  return text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/).map((piece, index) =>
    index % 2 === 0 ? (
      piece
    ) : piece.startsWith("`") ? (
      <code key={index}>{piece.slice(1, -1)}</code>
    ) : (
      <strong key={index}>{piece.slice(2, -2)}</strong>
    ),
  );
}

function Truncated({ length }: { length: number }) {
  return <p className="subtle chat-note">Cut short here; the full text was {formatNumber(length)} characters. The request's trace has all of it.</p>;
}

function RequestMeta({ id, record, sessionId }: { id: string; record?: RequestRecord; sessionId: string }) {
  const parts = [`Request #${id}`];
  if (record) {
    if (record.input_tokens || record.output_tokens) parts.push(`${tokens(record, record.input_tokens)} in, ${tokens(record, record.output_tokens)} out`);
    parts.push(formatDuration(record.total_ms));
  }
  return (
    <Link className="chat-meta subtle" to={requestPath({ session_id: sessionId, request_id: id })}>
      {parts.join(" · ")}
    </Link>
  );
}

function NoTraces({ session, calls }: { session: Session; calls: number }) {
  const client = useQueryClient();
  const notify = useToast();
  const enable = useMutation({
    mutationFn: () => api.setTrace(session.session_id, true),
    onSuccess: () => {
      notify("Tracing enabled for future requests");
      client.invalidateQueries({ queryKey: ["session", session.session_id] });
    },
    onError: (error) => notify(error.message),
  });
  return (
    <div className="empty">
      <p>
        {calls
          ? "The chat is read from captured payloads, and none of this session's model calls were captured."
          : "No model calls yet. The chat appears here once the agent talks to its model."}
      </p>
      {session.trace ? (
        calls > 0 && <p>Tracing is on now, so the next calls will show here.</p>
      ) : (
        <button type="button" className="primary" disabled={enable.isPending} onClick={() => enable.mutate()}>
          Capture payloads
        </button>
      )}
    </div>
  );
}

const TOOL_ARGS = ["command", "file_path", "path", "filePath", "pattern", "query", "url", "description", "prompt", "cmd"];

function toolSummary(input: unknown): string {
  if (typeof input === "string") return input.slice(0, 120);
  if (input && typeof input === "object") {
    const record = input as Record<string, unknown>;
    for (const key of TOOL_ARGS) {
      const value = record[key];
      if (typeof value === "string" && value) return value.split("\n")[0].slice(0, 120);
      if (Array.isArray(value) && value.length) return value.map(String).join(" ").slice(0, 120);
    }
    return JSON.stringify(input).slice(0, 120);
  }
  return "";
}

const peek = (text: string) => text.split(/\s+/).join(" ").trim().slice(0, 90);
