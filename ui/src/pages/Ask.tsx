import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api, askModel, preferredModel, providerModels, type AskMessage } from "../api";
import { useModels, useToast } from "../hooks";
import { Icon } from "../components/Icon";
import { PartTag } from "../components/PartTag";
import { Prose } from "../components/SessionChat";
import { providerLabel } from "../routes";

// Hosted APIs need the agent's own sign-in, which the console doesn't have.
const ASKABLE = new Set(["ollama", "strata", "docker"]);
// What the session is called on Sessions and Traffic.
const CLIENT = "console-chat";

interface Message extends AskMessage {
  reasoning?: string;
  at: string;
  failed?: string;
}

/**
 * A plain chat with one of the server's models, without an agent: the question goes through the
 * server the way an agent's call does (a session of its own, translated and tuned for Ollama,
 * recorded with its payloads), so it also shows on Sessions, Traffic and the session's chat.
 */
export function AskPage() {
  const models = useModels();
  const notify = useToast();
  const client = useQueryClient();
  const choices = useMemo(() => {
    const result: { backend: string; type: string; models: string[] }[] = [];
    for (const [backend, listing] of Object.entries(models.data?.backends ?? {})) {
      const type = listing.type ?? "ollama";
      if (!ASKABLE.has(type) || !listing.online) continue;
      const names = providerModels(listing, models.data?.aliases, type);
      if (names.length) result.push({ backend, type, models: names });
    }
    return result;
  }, [models.data]);
  const [picked, setPicked] = useState<{ backend: string; model: string } | null>(null);
  const choice = picked ?? (choices[0] ? { backend: choices[0].backend, model: preferredModel(choices[0].models) } : null);
  const [system, setSystem] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<{ backend: string; model: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const stop = useRef<AbortController | null>(null);
  const pane = useRef<HTMLDivElement>(null);
  const opened = useRef<string | null>(null);
  opened.current = sessionId;

  // The session ends when you start a new chat or leave the page.
  useEffect(() => {
    const end = () => {
      if (opened.current) api.endSession(opened.current).catch(() => undefined);
    };
    window.addEventListener("pagehide", end);
    return () => {
      window.removeEventListener("pagehide", end);
      stop.current?.abort();
      end();
    };
  }, []);
  useEffect(() => {
    const element = pane.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messages]);

  const reset = () => {
    stop.current?.abort();
    if (sessionId) api.endSession(sessionId).catch(() => undefined);
    setSessionId(null);
    setSession(null);
    setMessages([]);
    client.invalidateQueries({ queryKey: ["sessions"] });
  };

  // A different model is a different conversation: the session names its backend and model.
  const choose = (value: string) => {
    const [backend, ...rest] = value.split("\n");
    setPicked({ backend, model: rest.join("\n") });
    if (messages.length) reset();
  };

  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || busy || !choice) return;
    const history: Message[] = [...messages.filter((message) => !message.failed), { role: "user", content: text, at: new Date().toISOString() }];
    setMessages([...history, { role: "assistant", content: "", at: new Date().toISOString() }]);
    setDraft("");
    setBusy(true);
    const controller = new AbortController();
    stop.current = controller;
    try {
      let id = sessionId;
      if (!id || session?.backend !== choice.backend || session?.model !== choice.model) {
        if (id) api.endSession(id).catch(() => undefined);
        const created = await api.createSession({ client: CLIENT, model: choice.model, backend: choice.backend, trace: true });
        id = created.session_id;
        setSessionId(id);
        setSession(choice);
        client.invalidateQueries({ queryKey: ["sessions"] });
      }
      const conversation: AskMessage[] = [
        ...(system.trim() ? [{ role: "system" as const, content: system.trim() }] : []),
        ...history.map(({ role, content }) => ({ role, content })),
      ];
      await askModel(
        id,
        { model: choice.model, messages: conversation },
        (piece) =>
          setMessages((current) => {
            const last = current[current.length - 1];
            const next = { ...last, content: last.content + (piece.content ?? ""), reasoning: (last.reasoning ?? "") + (piece.reasoning ?? "") };
            return [...current.slice(0, -1), next];
          }),
        controller.signal,
      );
    } catch (error) {
      const stopped = controller.signal.aborted;
      setMessages((current) => {
        const last = current[current.length - 1];
        if (stopped) return last.content ? current : current.slice(0, -1);
        return [...current.slice(0, -1), { ...last, failed: error instanceof Error ? error.message : String(error) }];
      });
      if (!stopped) notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
      stop.current = null;
    }
  };

  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter starts a new line, as in most chats.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    }
  };

  const value = choice ? `${choice.backend}\n${choice.model}` : "";
  return (
    <div className="page wide">
      <header className="page-head">
        <PartTag />
        <h1>Ask a model</h1>
        <p className="lede">
          A plain chat with one of your models, without an agent. Each question goes through the server like an agent's call, so
          it's recorded as a session of its own.
        </p>
      </header>
      {models.isError && <p className="notice bad">{models.error.message}</p>}
      {models.isSuccess && !choices.length && (
        <p className="empty">
          No model is available. Local models come from your machines: add a model server on a machine's page, then pull a model
          there. <Link to="/machines">All machines</Link>
        </p>
      )}
      {choices.length > 0 && (
        <section className="ask">
          <div className="filters ask-bar">
            <label className="ask-model">
              <span>Model</span>
              <select value={value} onChange={(event) => choose(event.target.value)} disabled={busy}>
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
            <button type="button" className="small" onClick={reset} disabled={!messages.length}>
              <Icon name="plus" size={14} /> New chat
            </button>
            {sessionId && (
              <Link to={`/sessions/${sessionId}?view=chat`} className="subtle">
                Open as a session
              </Link>
            )}
          </div>
          <details className="ask-system">
            <summary>System prompt{system.trim() ? ": set" : " (optional)"}</summary>
            <textarea rows={3} value={system} onChange={(event) => setSystem(event.target.value)} placeholder="You are a helpful assistant." />
          </details>
          <div className="chat-scroll ask-scroll" ref={pane}>
            {!messages.length ? (
              <p className="empty ask-empty">Ask {choice?.model ?? "a model"} anything. Enter sends; Shift+Enter starts a new line.</p>
            ) : (
              <ol className="chat-list">
                {messages.map((message, index) =>
                  message.role === "user" ? (
                    <li key={index} className="chat-row chat-out chat-from-person">
                      <div className="chat-bubble">
                        <div className="chat-head">
                          <strong>You</strong>
                          <AskTime at={message.at} />
                        </div>
                        <Prose text={message.content} />
                      </div>
                    </li>
                  ) : (
                    <li key={index} className="chat-row chat-in chat-from-assistant">
                      <div className="chat-bubble">
                        <div className="chat-head">
                          <strong>{session?.model ?? choice?.model}</strong>
                          {busy && index === messages.length - 1 && (
                            <span className="chat-typing" aria-label="writing">
                              <i />
                              <i />
                              <i />
                            </span>
                          )}
                          <AskTime at={message.at} />
                        </div>
                        {message.reasoning && (
                          <details className="chat-fold chat-thinking">
                            <summary>Thinking</summary>
                            <Prose text={message.reasoning} />
                          </details>
                        )}
                        {message.content && <Prose text={message.content} />}
                        {message.failed && <p className="bad chat-small">{message.failed}</p>}
                      </div>
                    </li>
                  ),
                )}
              </ol>
            )}
          </div>
          <form className="ask-form" onSubmit={send}>
            <textarea
              aria-label="Your message"
              rows={2}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKey}
              placeholder={`Message ${choice?.model ?? ""}`}
            />
            {busy ? (
              <button type="button" onClick={() => stop.current?.abort()}>
                Stop
              </button>
            ) : (
              <button type="submit" className="primary" disabled={!draft.trim()}>
                Send
              </button>
            )}
          </form>
        </section>
      )}
    </div>
  );
}

function AskTime({ at }: { at: string }) {
  return <time className="chat-time">{new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>;
}
