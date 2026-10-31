import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import {
  api,
  tokens,
  type ChatAuthor,
  type ChatBlock,
  type ChatKind,
  type ChatThread,
  type ChatTurn,
  type RequestRecord,
  type Session,
  type SessionChat as ChatPage,
} from "../api";
import { formatDate, formatDuration, formatNumber, formatRelative } from "../format";
import { useAgents, useToast } from "../hooks";
import type { LiveRequest } from "../live";
import { Icon } from "./Icon";
import { requestPath } from "./RequestList";

const PAGE = 60;
// Older messages load once the top of the pane is this close.
const LOAD_MARGIN = "600px";
// A run of more tool calls than this in one reply is folded into a single line.
const OPEN_TOOLS = 3;

/** Where a block lives, so a preview can ask the server for its whole turn. */
interface Place {
  turn: number;
  position: number;
}

type Part = { block: ChatBlock } & Place;

/** What to call the people in a chat: the person (You, or the owner's name on a shared session) and the agent. */
interface Names {
  person: string;
  agent: string;
}

const ChatSource = createContext<{ sessionId: string; thread: string }>({ sessionId: "", thread: "" });

/**
 * The session read back as a chat, the way the agent's own window shows it: built by the server
 * from the captured calls of each conversation, each reply linked to the request that wrote it.
 *
 * Agents send the model far more as "the user" than the person typed: reminders, tool output, their own
 * background calls (a title, a recap, a suggested next prompt). The server says who wrote each piece, and
 * this keeps them apart: the person's messages on the right, the agent's replies on the left, what the
 * agent added itself as grey notes in the middle (most hidden unless asked for), and every background
 * call and sub-agent in a list of its own rather than in the chat.
 */
export function SessionChat({ session, records, streaming }: { session: Session; records: RequestRecord[]; streaming: LiveRequest[] }) {
  const [thread, setThread] = useState<string | null>(null);
  const [context, setContext] = useState(false);
  const [handed, setHanded] = useState(false);
  const agents = useAgents();
  const names: Names = {
    person: session.you && !session.you.owner ? (session.you.sharing.owner ?? "The user") : "You",
    agent: agents.data?.find((agent) => agent.id === session.client)?.name ?? session.client,
  };
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
  const kind: ChatKind = first?.kind ?? "main";
  const inherited = first?.inherited ?? 0;
  const turns = useMemo(() => {
    const byIndex = new Map<number, ChatTurn>();
    // Later pages are older; the newest copy of a turn wins.
    for (const page of [...(pages ?? [])].reverse()) for (const turn of page.turns) byIndex.set(turn.index, turn);
    const total = pages?.[0]?.total ?? 0;
    return [...byIndex.values()].filter((turn) => turn.index < total && (handed || !turn.inherited)).sort((a, b) => a.index - b.index);
  }, [pages, handed]);
  const byId = useMemo(() => new Map(records.map((record) => [record.request_id, record])), [records]);
  const results = useMemo(() => {
    const map = new Map<string, { block: ToolResult } & Place>();
    for (const turn of turns)
      turn.blocks.forEach((block, position) => {
        if (block.type === "tool_result" && block.tool_use_id) map.set(block.tool_use_id, { block, turn: turn.index, position });
      });
    return map;
  }, [turns]);
  const items = useMemo(() => {
    const paired = new Set(turns.flatMap((turn) => turn.blocks.flatMap((block) => (block.type === "tool_use" && results.has(block.id) ? [block.id] : []))));
    return buildItems(turns, paired, context, kind, names);
  }, [turns, results, context, kind, names.person, names.agent]);

  const current = first?.threads.find((item) => item.id === first.thread);
  const live = current?.main && !session.ended_at ? streaming : [];
  const view = first ? `${session.session_id}:${first.thread}:${handed}` : "";
  const scroll = useStickyScroll(view, turns[0]?.index ?? 0, items.length + live.length);
  const pane = scroll.pane;
  const top = useRef<HTMLDivElement>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = chat;
  // While what a sub-agent or background call was handed is hidden, there's nothing older to load.
  const more = Boolean(hasNextPage) && (handed || (turns[0]?.index ?? 0) > inherited);
  useEffect(() => {
    const target = top.current;
    if (!target || !more) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting) && !isFetchingNextPage) fetchNextPage();
      },
      { root: pane.current, rootMargin: `${LOAD_MARGIN} 0px 0px 0px` },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [more, isFetchingNextPage, fetchNextPage, pane, turns.length]);

  // Steps between the person's own messages: what they typed is what people come here to find.
  const jump = useCallback(
    (direction: -1 | 1) => {
      const element = pane.current;
      if (!element) return;
      const mine = [...element.querySelectorAll<HTMLElement>(".chat-from-person")];
      const at = element.scrollTop;
      const target = direction < 0 ? mine.filter((node) => node.offsetTop < at - 8).pop() : mine.find((node) => node.offsetTop > at + 8);
      if (target) element.scrollTo({ top: target.offsetTop - 12, behavior: "smooth" });
      else if (direction < 0 && more) fetchNextPage();
    },
    [pane, more, fetchNextPage],
  );

  if (chat.isPending) return <p className="empty">Reading the captured requests…</p>;
  if (chat.isError) return <p className="notice bad">{chat.error.message}</p>;
  if (!first || !first.traced_calls) return <NoTraces session={session} calls={first?.model_calls ?? 0} />;

  const choose = (id: string) => {
    setThread(id);
    setHanded(false);
  };
  const several = first.threads.length > 1;
  const conversational = kind === "main" || kind === "conversation";
  return (
    <div className={`chat ${several ? "with-threads" : ""}`}>
      {several && (
        <aside className="chat-side">
          <ThreadList threads={first.threads} value={first.thread} onChange={choose} names={names} />
        </aside>
      )}
      <div className="chat-main">
        {current && <ChatHeader thread={current} kind={kind} names={names} />}
        <div className="filters chat-filters">
          {several && <ThreadPicker threads={first.threads} value={first.thread} onChange={choose} />}
          <label className="check">
            <input type="checkbox" checked={context} onChange={(event) => setContext(event.target.checked)} />
            Show what {names.agent} added
          </label>
          {conversational && (
            <span className="chat-mine">
              <button type="button" className="small" onClick={() => jump(-1)} title={`Your previous message`}>
                <Icon name="up" size={14} /> {names.person === "You" ? "My" : "Their"} previous
              </button>
              <button type="button" className="small" onClick={() => jump(1)} title="Your next message">
                <Icon name="up" size={14} className="flip" /> Next
              </button>
            </span>
          )}
          {first.traced_calls < first.model_calls && (
            <span className="subtle">
              {formatNumber(first.traced_calls)} of {formatNumber(first.model_calls)} model calls were captured
            </span>
          )}
        </div>
        <ChatSource.Provider value={{ sessionId: session.session_id, thread: first.thread ?? "" }}>
          <div className="chat-frame">
            <div className={`chat-scroll chat-is-${kind}`} ref={pane}>
              <div ref={top} className="chat-top subtle">
                {inherited > 0 && !handed ? (
                  <button type="button" className="chat-handed" onClick={() => setHanded(true)}>
                    {kind === "background"
                      ? `Show the ${formatNumber(inherited)} earlier messages it was sent with`
                      : `Show the ${formatNumber(inherited)} messages it was handed from the main conversation`}
                  </button>
                ) : more ? (
                  isFetchingNextPage ? (
                    "Loading earlier messages…"
                  ) : (
                    `${formatNumber(turns[0]?.index ?? 0)} earlier messages`
                  )
                ) : (
                  "Start of the conversation"
                )}
              </div>
              <ol className="chat-list">
                {items.map((item) => (
                  <ChatItem key={item.key} item={item} kind={kind} names={names} results={results} sessionId={session.session_id} records={byId} />
                ))}
                {live.map((entry) => (
                  <li key={entry.key} className="chat-row chat-in">
                    <Avatar who="assistant" names={names} />
                    <div className="chat-bubble">
                      <div className="chat-head">
                        <strong>{names.agent}</strong>
                        <span className="chat-typing" aria-label="writing">
                          <i />
                          <i />
                          <i />
                        </span>
                      </div>
                      {entry.reasoning && <Fold label="Thinking" className="chat-thinking" text={entry.reasoning} />}
                      {entry.content && <Prose text={entry.content} />}
                      {entry.tool && <pre className="chat-code wrap">{entry.tool}</pre>}
                      <Link className="chat-meta subtle" to={requestPath(entry)}>
                        Request #{entry.request_id}, streaming
                      </Link>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
            {!scroll.atBottom && (
              <button type="button" className="chat-jump" onClick={scroll.toBottom}>
                <Icon name="up" size={14} className="flip" />
                {scroll.unseen > 0 ? "New messages" : "Newest"}
              </button>
            )}
          </div>
        </ChatSource.Provider>
      </div>
    </div>
  );
}

/**
 * The chat pane's scrolling: it opens at the bottom, stays there as new messages arrive while you're
 * at the bottom, and keeps your place when older messages are put in above.
 */
function useStickyScroll(view: string, oldest: number, count: number) {
  const pane = useRef<HTMLDivElement>(null);
  const state = useRef({ view: "", oldest: 0, fromBottom: 0, atBottom: true, seen: 0 });
  const [atBottom, setAtBottom] = useState(true);
  const [seen, setSeen] = useState(0);
  useEffect(() => {
    const element = pane.current;
    if (!element) return;
    const onScroll = () => {
      state.current.fromBottom = element.scrollHeight - element.scrollTop;
      const bottom = element.scrollHeight - element.scrollTop - element.clientHeight < 40;
      state.current.atBottom = bottom;
      setAtBottom(bottom);
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
  // What arrived while you were scrolled up, so the button can say there's something new.
  useEffect(() => {
    if (atBottom) setSeen(count);
  }, [atBottom, count]);
  const toBottom = useCallback(() => {
    const element = pane.current;
    if (element) element.scrollTo({ top: element.scrollHeight, behavior: "smooth" });
  }, []);
  return { pane, atBottom, unseen: Math.max(0, count - seen), toBottom };
}

const KIND_LABELS: Record<ChatKind, string> = {
  main: "Main conversation",
  conversation: "Conversation",
  subagent: "Sub-agent",
  background: "Background call",
};

/** What the open conversation is, and whose words are in it. */
function ChatHeader({ thread, kind, names }: { thread: ChatThread; kind: ChatKind; names: Names }) {
  const you = names.person === "You";
  const about = {
    main: `${you ? "Your" : `${names.person}'s`} messages are on the right. What ${names.agent} added itself is shown as grey notes in the middle, or hidden.`,
    conversation: `Another conversation in this session, such as one from before /clear.`,
    subagent: `A helper ${names.agent} started for one task. What it was asked came from the main agent, not from ${you ? "you" : names.person}.`,
    background: `${names.agent} made this call by itself, on the side. Nothing in it was typed by ${you ? "you" : names.person}, and it isn't part of the conversation.`,
  }[kind];
  const heading = kind === "background" ? thread.purpose || thread.title || "Background call" : thread.title || KIND_LABELS[kind];
  return (
    <header className={`chat-header chat-header-${kind}`}>
      <div className="chat-header-line">
        <span className={`chat-kind chat-kind-${kind}`}>{KIND_LABELS[kind]}</span>
        <h3>{heading}</h3>
      </div>
      {kind === "background" && thread.purpose && thread.title && <p className="chat-header-title">{thread.title}</p>}
      <p className="subtle chat-about">
        {about} {thread.model || "Unknown model"}, {formatNumber(thread.requests.length)} {thread.requests.length === 1 ? "call" : "calls"}, last{" "}
        {formatRelative(thread.updated)}.
      </p>
    </header>
  );
}

const newestFirst = (a: ChatThread, b: ChatThread) => (a.updated < b.updated ? 1 : a.updated > b.updated ? -1 : 0);

function groupThreads(threads: ChatThread[]) {
  return {
    main: threads.filter((thread) => thread.main),
    conversations: threads.filter((thread) => !thread.main && thread.kind === "conversation").sort(newestFirst),
    subagents: threads.filter((thread) => !thread.main && thread.kind === "subagent").sort(newestFirst),
    background: threads.filter((thread) => !thread.main && thread.kind === "background").sort(newestFirst),
  };
}

const threadLabel = (thread: ChatThread) =>
  thread.main ? "Main conversation" : thread.kind === "background" ? thread.purpose || thread.title || "Background call" : thread.title || "Untitled";

/** The session's conversations, sub-agents and background calls, kept apart so the chat holds only the conversation. */
function ThreadList({ threads, value, onChange, names }: { threads: ChatThread[]; value: string | null; onChange: (id: string) => void; names: Names }) {
  const groups = groupThreads(threads);
  const selected = threads.find((thread) => thread.id === value);
  const item = (thread: ChatThread) => (
    <li key={thread.id}>
      <button type="button" className={`chat-thread ${thread.id === value ? "active" : ""}`} aria-current={thread.id === value} onClick={() => onChange(thread.id)}>
        <span className="chat-thread-title">{threadLabel(thread)}</span>
        {(thread.main || (thread.kind === "background" && thread.purpose)) && thread.title && <span className="chat-thread-sub">{thread.title}</span>}
        <span className="chat-thread-meta">
          {formatNumber(thread.requests.length)} {thread.requests.length === 1 ? "call" : "calls"} · {formatRelative(thread.updated)}
        </span>
      </button>
    </li>
  );
  return (
    <nav className="chat-threads" aria-label="Conversations in this session">
      <ul>{[...groups.main, ...groups.conversations].map(item)}</ul>
      {groups.subagents.length > 0 && (
        <>
          <h4>Sub-agents ({groups.subagents.length})</h4>
          <ul>{groups.subagents.map(item)}</ul>
        </>
      )}
      {groups.background.length > 0 && (
        <details className="chat-background" open={selected?.kind === "background" || undefined}>
          <summary>Background calls ({groups.background.length})</summary>
          <p className="subtle">Calls {names.agent} made by itself: titles, recaps, suggested prompts, safety checks.</p>
          <ul>{groups.background.map(item)}</ul>
        </details>
      )}
    </nav>
  );
}

/** The same list as a select, for narrow screens where the side list doesn't fit. */
function ThreadPicker({ threads, value, onChange }: { threads: ChatThread[]; value: string | null; onChange: (id: string) => void }) {
  const groups = groupThreads(threads);
  const option = (thread: ChatThread) => (
    <option key={thread.id} value={thread.id}>
      {threadLabel(thread)} ({thread.requests.length} {thread.requests.length === 1 ? "call" : "calls"})
    </option>
  );
  return (
    <select className="chat-picker" aria-label="Conversation" value={value ?? ""} onChange={(event) => onChange(event.target.value)}>
      <optgroup label="Conversations">{[...groups.main, ...groups.conversations].map(option)}</optgroup>
      {groups.subagents.length > 0 && <optgroup label="Sub-agents">{groups.subagents.map(option)}</optgroup>}
      {groups.background.length > 0 && <optgroup label="Background calls">{groups.background.map(option)}</optgroup>}
    </select>
  );
}

type ToolResult = Extract<ChatBlock, { type: "tool_result" }>;
type Results = Map<string, { block: ToolResult } & Place>;
type Step = { parts: Part[]; request_id?: string; at?: string };

/** What the chat shows, in order: day lines, messages sent as the user, the agent's replies, and notes in between. */
type Item =
  | { type: "day"; key: string; label: string }
  | { type: "out"; key: string; who: ChatAuthor; at?: string; parts: Part[] }
  | { type: "in"; key: string; tool?: boolean; at?: string; steps: Step[] }
  | { type: "note"; key: string; label: string; at?: string; part?: Part; text?: string; tone: "event" | "context" };

function buildItems(turns: ChatTurn[], paired: Set<string>, context: boolean, kind: ChatKind, names: Names): Item[] {
  const items: Item[] = [];
  let day = "";
  const push = (item: Item, at?: string) => {
    const date = at ? new Date(at) : null;
    if (date && !Number.isNaN(date.getTime()) && date.toDateString() !== day) {
      day = date.toDateString();
      items.push({ type: "day", key: `day-${item.key}`, label: dayLabel(date) });
    }
    items.push(item);
  };
  // Old indexes said nothing about authors: the main chat's user is the person, elsewhere nobody knows.
  const fallback: ChatAuthor = kind === "main" || kind === "conversation" ? "person" : "unknown";
  for (const turn of turns) {
    const parts: Part[] = turn.blocks.map((block, position) => ({ block, turn: turn.index, position }));
    const key = `t${turn.index}`;
    if (turn.role === "system") {
      const text = parts.map(({ block }) => ("text" in block ? block.text : "")).join("\n\n");
      if (turn.event === "compacted") push({ type: "note", key, label: "Conversation compacted", text, tone: "event" }, turn.at);
      else if (turn.preamble || context)
        push({ type: "note", key, label: turn.preamble ? "System prompt" : "System message", part: parts.length === 1 ? parts[0] : undefined, text, tone: "context" }, turn.at);
      continue;
    }
    if (turn.role === "assistant") {
      // Claude Code sends its thinking back without the text; there's nothing to show.
      const shown = parts.filter(({ block }) => !(block.type === "thinking" && !block.text));
      if (!shown.length) continue;
      const last = items[items.length - 1];
      const step = { parts: shown, request_id: turn.request_id, at: turn.at };
      if (last?.type === "in" && !last.tool) last.steps.push(step);
      else push({ type: "in", key, at: turn.at, steps: [step] }, turn.at);
      continue;
    }
    if (turn.role === "tool") {
      // Tool results shown inside their call aren't repeated on their own.
      const shown = parts.filter(({ block }) => !(block.type === "tool_result" && paired.has(block.tool_use_id)));
      if (shown.length) push({ type: "in", key, tool: true, at: turn.at, steps: [{ parts: shown }] }, turn.at);
      continue;
    }
    let run: Part[] = [];
    let who: ChatAuthor | null = null;
    const flush = () => {
      if (run.length && who) push({ type: "out", key: `${key}-${run[0].position}`, who, at: turn.at, parts: run }, turn.at);
      run = [];
      who = null;
    };
    for (const part of parts) {
      const { block } = part;
      if (block.type === "tool_result" && paired.has(block.tool_use_id)) continue;
      const author: ChatAuthor = block.type === "text" ? (block.author ?? (block.reminder ? "agent" : fallback)) : (who ?? fallback);
      // The agent's own text is a note, except in a background call, where its instructions are the message.
      if (block.type === "text" && author === "agent" && (block.note || block.reminder || kind !== "background")) {
        flush();
        const note = noteItem(part, block, context, names, kind);
        if (note) push(note, turn.at);
        continue;
      }
      if (who && who !== author) flush();
      who = author;
      run.push(part);
    }
    flush();
  }
  return items;
}

function noteItem(part: Part, block: Extract<ChatBlock, { type: "text" }>, context: boolean, names: Names, kind: ChatKind): Item | null {
  const key = `t${part.turn}-${part.position}`;
  switch (block.note) {
    case "interrupted":
      return { type: "note", key, label: kind === "main" ? `${names.person} interrupted the agent` : "Interrupted", tone: "event" };
    case "summary":
      return { type: "note", key, label: "Summary of the earlier conversation", part, text: block.text, tone: "event" };
    case "output":
      // A command's output, written by the agent for the person; empty ones say nothing.
      if (!block.text.replace(/<\/?[\w-]+>/g, "").trim()) return null;
      return { type: "note", key, label: "Command output", part, text: block.text, tone: "event" };
    default:
      if (!context) return null;
      return { type: "note", key, label: `Added by ${names.agent}${block.note === "reminder" ? ": reminder" : ""}`, part, text: block.text, tone: "context" };
  }
}

function dayLabel(date: Date): string {
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: date.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}

function Time({ at }: { at?: string }) {
  if (!at) return null;
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return null;
  return (
    <time className="chat-time" dateTime={at} title={formatDate(at)}>
      {date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
    </time>
  );
}

type Speaker = ChatAuthor | "assistant" | "tool";

function Avatar({ who, names }: { who: Speaker; names: Names }) {
  const letter =
    who === "person" ? (names.person === "You" ? "Y" : names.person[0]) : who === "assistant" ? names.agent[0] : who === "parent" ? "M" : who === "tool" ? "⚙" : "?";
  return (
    <span className={`chat-avatar chat-avatar-${who}`} aria-hidden="true">
      {who === "agent" ? <Icon name="cpu" size={14} /> : (letter ?? "?").toUpperCase()}
    </span>
  );
}

function outLabel(who: ChatAuthor, kind: ChatKind, names: Names): string {
  if (who === "person") return names.person;
  if (who === "parent") return "Main agent";
  if (who === "agent") return kind === "background" ? `${names.agent}, automatically` : names.agent;
  return "Sent as the user";
}

function ChatItem({ item, kind, names, results, sessionId, records }: { item: Item; kind: ChatKind; names: Names; results: Results; sessionId: string; records: Map<string, RequestRecord> }) {
  if (item.type === "day")
    return (
      <li className="chat-day" role="separator">
        <span>{item.label}</span>
      </li>
    );
  if (item.type === "note") {
    return (
      <li className={`chat-note-row chat-note-${item.tone}`}>
        {item.text || item.part ? (
          <Fold label={item.label} text={item.text ?? ""} mono place={item.part} preview={item.part?.block.preview} className="chat-note-fold" />
        ) : (
          <span className="chat-note-text">{item.label}</span>
        )}
        <Time at={item.at} />
      </li>
    );
  }
  if (item.type === "out") {
    const queued = item.parts.some(({ block }) => block.type === "text" && block.queued);
    return (
      <li className={`chat-row chat-out chat-from-${item.who}`}>
        <div className="chat-bubble">
          <div className="chat-head">
            <strong>{outLabel(item.who, kind, names)}</strong>
            {queued && <span className="chat-flag">sent while it was working</span>}
            {item.who === "unknown" && <span className="chat-flag">you or the agent</span>}
            <Time at={item.at} />
          </div>
          {item.parts.map((part) => (
            <OutBlock key={`${part.turn}-${part.position}`} part={part} folded={item.who === "agent" || item.who === "unknown"} />
          ))}
        </div>
        <Avatar who={item.who} names={names} />
      </li>
    );
  }
  const label = item.tool ? "Tool output" : kind === "subagent" ? "Sub-agent" : kind === "background" ? "Answer" : names.agent;
  return (
    <li className={`chat-row chat-in ${item.tool ? "chat-from-tool" : "chat-from-assistant"}`}>
      <Avatar who={item.tool ? "tool" : "assistant"} names={names} />
      <div className="chat-bubble">
        <div className="chat-head">
          <strong>{label}</strong>
          <Time at={item.at} />
        </div>
        <Reply steps={item.steps} results={results} sessionId={sessionId} records={records} />
      </div>
    </li>
  );
}

/** Something sent as the user: what a person typed in full (a command as a command, a paste folded), the agent's own folded. */
function OutBlock({ part, folded }: { part: Part; folded: boolean }) {
  const { block } = part;
  if (block.type === "image") return <p className="subtle chat-small">[image]</p>;
  if (block.type !== "text") return <Fold label="Other content" text={"text" in block ? block.text : ""} mono place={part} preview={block.preview} />;
  if (block.command) return <code className="chat-command">{block.text}</code>;
  if (folded && (block.preview || block.text.length > 600)) return <Fold label={peek(block.text, 70)} text={block.text} place={part} preview={block.preview} truncated={block.truncated} />;
  // Claude Code wraps what's pasted into it; it reads better folded away than as a wall of log.
  const pieces = block.text.split(/<pasted_content[^>]*>([\s\S]*?)<\/pasted_content>/);
  return (
    <>
      {pieces.map((piece, index) =>
        index % 2 ? (
          <Fold key={index} label={`Pasted text, ${formatNumber(piece.trim().split("\n").length)} lines`} text={piece.trim()} mono className="chat-paste" />
        ) : (
          piece.trim() && <Prose key={index} text={piece.trim()} />
        ),
      )}
      {block.truncated && <Truncated length={block.truncated} />}
    </>
  );
}

type Chunk = { type: "part"; part: Part } | { type: "tools"; calls: { part: Part; request_id?: string }[] } | { type: "meta"; request_id: string };

/** The agent's reply, from one or more calls: its text, its thinking, and runs of tool calls folded together. */
function Reply({ steps, results, sessionId, records }: { steps: Step[]; results: Results; sessionId: string; records: Map<string, RequestRecord> }) {
  const chunks: Chunk[] = [];
  for (const step of steps) {
    let spoke = false;
    for (const part of step.parts) {
      const last = chunks[chunks.length - 1];
      if (part.block.type === "tool_use") {
        if (last?.type === "tools") last.calls.push({ part, request_id: step.request_id });
        else chunks.push({ type: "tools", calls: [{ part, request_id: step.request_id }] });
      } else {
        chunks.push({ type: "part", part });
        if (part.block.type === "text" || part.block.type === "error") spoke = true;
      }
    }
    // A call's tokens and time go under what it said; a call that only used tools has them in each tool's details.
    if (spoke && step.request_id) chunks.push({ type: "meta", request_id: step.request_id });
  }
  return (
    <div className="chat-reply">
      {chunks.map((chunk, index) =>
        chunk.type === "meta" ? (
          <RequestMeta key={index} id={chunk.request_id} record={records.get(chunk.request_id)} sessionId={sessionId} />
        ) : chunk.type === "tools" ? (
          <ToolRun key={index} calls={chunk.calls} results={results} sessionId={sessionId} />
        ) : (
          <Block key={index} item={chunk.part} />
        ),
      )}
    </div>
  );
}

function ToolRun({ calls, results, sessionId }: { calls: { part: Part; request_id?: string }[]; results: Results; sessionId: string }) {
  const list = (
    <div className="chat-tool-list">
      {calls.map(({ part, request_id }) => {
        const block = part.block as Extract<ChatBlock, { type: "tool_use" }>;
        return <ToolCall key={`${part.turn}-${part.position}`} block={block} place={part} result={results.get(block.id)} requestId={request_id} sessionId={sessionId} />;
      })}
    </div>
  );
  if (calls.length <= OPEN_TOOLS) return list;
  const counts = new Map<string, number>();
  for (const { part } of calls) if (part.block.type === "tool_use") counts.set(part.block.name, (counts.get(part.block.name) ?? 0) + 1);
  const failed = calls.filter(({ part }) => part.block.type === "tool_use" && results.get(part.block.id)?.block.is_error).length;
  return (
    <details className="chat-tool-run">
      <summary>
        <span>Used {formatNumber(calls.length)} tools</span>
        <span className="subtle chat-tool-arg">
          {[...counts].map(([name, count]) => (count > 1 ? `${name} ×${count}` : name)).join(" · ")}
        </span>
        {failed > 0 && <span className="pill bad">{failed} failed</span>}
      </summary>
      {list}
    </details>
  );
}

function Block({ item }: { item: Part }) {
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
      return <p className="subtle chat-small">[image]</p>;
    case "error":
      return <pre className="chat-code wrap chat-error-text">{block.text}</pre>;
    case "tool_use":
      return null;
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

function ToolCall({
  block,
  place,
  result,
  requestId,
  sessionId,
}: {
  block: Extract<ChatBlock, { type: "tool_use" }>;
  place: Place;
  result?: { block: ToolResult } & Place;
  requestId?: string;
  sessionId: string;
}) {
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
          {loading && <p className="subtle chat-small">Loading the full text…</p>}
          <div className="chat-tool-label">Input</div>
          <pre className="chat-code wrap">{input}</pre>
          {output && (
            <>
              <div className="chat-tool-label">{output.is_error ? "Error" : "Result"}</div>
              <pre className="chat-code wrap">{output.text || "(empty)"}</pre>
              {output.truncated && <Truncated length={output.truncated} />}
            </>
          )}
          {requestId && (
            <Link className="chat-meta subtle" to={requestPath({ session_id: sessionId, request_id: requestId })}>
              Request #{requestId}
            </Link>
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
        {!open && text && <span className="subtle chat-fold-peek">{peek(text)}</span>}
      </summary>
      {open && preview && !full && <p className="subtle chat-small">Loading the full text…</p>}
      {open && (mono ? <pre className="chat-code wrap">{shown}</pre> : <Prose text={shown} />)}
      {open && cut && <Truncated length={cut} />}
    </details>
  );
}

/** Markdown as agents write it, kept simple: code blocks, headings, lists, bold and inline code. */
export function Prose({ text, truncated }: { text: string; truncated?: number }) {
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
  return <p className="subtle chat-small">Cut short here; the full text was {formatNumber(length)} characters. The request's trace has all of it.</p>;
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

const peek = (text: string, length = 90) => text.split(/\s+/).join(" ").trim().slice(0, length);
