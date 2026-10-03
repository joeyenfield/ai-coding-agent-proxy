import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isFailure, type RequestRecord, type Session, type Trace } from "../api";
import { endpointPath, formatBytes, formatDate, formatDuration, formatNumber } from "../format";
import { download, useClipboard, useToast } from "../hooks";

type Tab = "request" | "response" | "conversation" | "upstream" | "metadata";

export function Inspector({ record, session }: { record: RequestRecord; session?: Session }) {
  const [fullscreen, setFullscreen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (fullscreen) dialog.current?.showModal();
    else dialog.current?.close();
  }, [fullscreen]);
  const body = <InspectorBody record={record} session={session} fullscreen={fullscreen} onFullscreen={setFullscreen} />;
  return (
    <>
      {fullscreen ? <div className="inspector placeholder">Shown in full screen</div> : body}
      <dialog ref={dialog} className="fullscreen" aria-label={`Request ${record.request_id}`} onClose={() => setFullscreen(false)}>
        {fullscreen && body}
      </dialog>
    </>
  );
}

function InspectorBody({
  record,
  session,
  fullscreen,
  onFullscreen,
}: {
  record: RequestRecord;
  session?: Session;
  fullscreen: boolean;
  onFullscreen: (value: boolean) => void;
}) {
  const trace = useQuery({
    queryKey: ["trace", record.session_id, record.request_id],
    queryFn: () => api.trace(record.session_id, record.request_id),
    enabled: Boolean(record.trace_available),
    staleTime: Infinity,
  });
  const failed = isFailure(record);
  return (
    <section className={`inspector ${fullscreen ? "is-fullscreen" : ""}`} aria-label="Request details">
      <header className="inspector-head">
        <div>
          <h2>{record.model || "Unknown model"}</h2>
          <p className="subtle">
            Request {record.request_id} from {record.client} on {record.backend}
          </p>
          <p className="mono subtle">{endpointPath(record.endpoint)}</p>
        </div>
        <div className="row">
          <span className={`pill ${failed ? "bad" : "good"}`}>{failed ? `Failed ${record.status}` : record.status}</span>
          <button type="button" onClick={() => onFullscreen(!fullscreen)}>
            {fullscreen ? "Exit full screen" : "Full screen"}
          </button>
        </div>
      </header>
      <dl className="metrics">
        <Metric label="Total time" value={formatDuration(record.total_ms)} />
        <Metric label="First token" value={formatDuration(record.ttft_ms)} />
        <Metric label="Prompt" value={`${formatNumber(record.input_tokens)} tok, ${formatNumber(record.prompt_tps)}/s`} />
        <Metric label="Output" value={`${formatNumber(record.output_tokens)} tok, ${formatNumber(record.generation_tps)}/s`} />
        <Metric label="Context" value={record.context_size ? formatNumber(record.context_size) : "server default"} />
        <Metric label="Payload" value={`${formatBytes(record.request_bytes)} in, ${formatBytes(record.response_bytes)} out`} />
      </dl>
      {!record.trace_available ? (
        <Untraced record={record} session={session} />
      ) : trace.isPending ? (
        <p className="notice">Loading the captured payload…</p>
      ) : trace.isError ? (
        <div className="notice bad">
          <p>{trace.error.message}</p>
          <button type="button" onClick={() => trace.refetch()}>
            Retry
          </button>
        </div>
      ) : (
        <TraceView record={record} trace={trace.data} />
      )}
      <footer className="inspector-foot subtle">
        {formatDate(record.timestamp)}, session <span className="mono">{record.session_id}</span>
      </footer>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function Untraced({ record, session }: { record: RequestRecord; session?: Session }) {
  const client = useQueryClient();
  const notify = useToast();
  const enable = useMutation({
    mutationFn: () => api.setTrace(record.session_id, true),
    onSuccess: () => {
      notify("Tracing enabled for future requests");
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
    onError: (error) => notify(error.message),
  });
  return (
    <div className="payload">
      <div className="notice">
        <h3>No payload captured</h3>
        <p>
          {session?.trace
            ? "Tracing is on, so the next requests in this session will be captured. Earlier payloads can't be recovered."
            : "Turn on tracing to capture the next requests in this session. Earlier payloads can't be recovered."}
        </p>
        {session && !session.trace && (
          <button type="button" className="primary" disabled={enable.isPending} onClick={() => enable.mutate()}>
            Enable tracing
          </button>
        )}
      </div>
      <pre className="code wrap">{JSON.stringify(record, null, 2)}</pre>
    </div>
  );
}

function TraceView({ record, trace }: { record: RequestRecord; trace: Trace }) {
  const [tab, setTab] = useState<Tab>("conversation");
  const [wrap, setWrap] = useState(true);
  const copy = useClipboard();
  const tabs: Tab[] = ["conversation", "request", "response", ...(trace.upstream_request ? (["upstream"] as Tab[]) : []), "metadata"];
  const text = useMemo(() => {
    if (tab === "conversation") return "";
    const value = tab === "upstream" ? trace.upstream_request : tab === "metadata" ? trace.metadata : trace[tab];
    return JSON.stringify(value, null, 2) ?? "null";
  }, [tab, trace]);
  const filename = (suffix: string) => `${record.session_id}-${record.request_id}-${suffix}.json`;
  return (
    <div className="payload">
      <div className="tabs" role="tablist" aria-label="Captured payload">
        {tabs.map((name) => (
          <button key={name} type="button" role="tab" aria-selected={tab === name} onClick={() => setTab(name)}>
            {TAB_LABELS[name]}
          </button>
        ))}
      </div>
      <div className="payload-bar">
        <span className="subtle">{tab === "conversation" ? "Readable view of the captured messages" : `${formatBytes(new TextEncoder().encode(text).length)}`}</span>
        <div className="row">
          {tab !== "conversation" && (
            <>
              <label className="check">
                <input type="checkbox" checked={wrap} onChange={(event) => setWrap(event.target.checked)} />
                Wrap
              </label>
              <button type="button" onClick={() => copy(text, "Payload copied")}>
                Copy JSON
              </button>
              <button type="button" onClick={() => download(text, filename(tab))}>
                Download
              </button>
            </>
          )}
          <button type="button" onClick={() => download(JSON.stringify(trace, null, 2), filename("trace"))}>
            Download full trace
          </button>
        </div>
      </div>
      <div className="payload-area" role="tabpanel">
        {tab === "conversation" ? <Conversation trace={trace} /> : <pre className={`code ${wrap ? "wrap" : ""}`}>{text}</pre>}
      </div>
    </div>
  );
}

const TAB_LABELS: Record<Tab, string> = {
  conversation: "Conversation",
  request: "Request",
  response: "Response",
  upstream: "Sent to Ollama",
  metadata: "Metadata",
};

function Conversation({ trace }: { trace: Trace }) {
  const entries = conversationEntries(trace);
  if (!entries.length) return <p className="empty">This trace has no message content.</p>;
  return (
    <ol className="conversation">
      {entries.map((entry, index) => (
        <li key={index} className={`turn turn-${entry.kind}`}>
          <details open={entry.kind !== "system" || entries.length < 4}>
            <summary>{entry.role}</summary>
            <pre className="wrap">{entry.content}</pre>
          </details>
        </li>
      ))}
    </ol>
  );
}

interface Entry {
  role: string;
  kind: "system" | "user" | "assistant" | "tool" | "reasoning" | "other";
  content: string;
}

type Json = Record<string, any>;

function conversationEntries(trace: Trace): Entry[] {
  const request = (trace.request ?? {}) as Json;
  const response = trace.response as Json | Json[] | undefined;
  const entries: Entry[] = [];
  const add = (role: string, content: unknown, kind?: Entry["kind"]) => {
    if (content == null || content === "") return;
    entries.push({
      role,
      kind: kind ?? kindOf(role),
      content: typeof content === "string" ? content : JSON.stringify(content, null, 2),
    });
  };
  add("System", flattenText(request.system), "system");
  add("Instructions", request.instructions, "system");
  for (const message of request.messages ?? []) {
    add(titleCase(message.role ?? "message"), flattenText(message.content ?? message));
    if (message.tool_calls) add("Tool calls", message.tool_calls, "tool");
  }
  add("Prompt", request.prompt, "user");
  if (Array.isArray(request.input)) {
    for (const item of request.input) add(titleCase(item.role ?? item.type ?? "input"), flattenText(item.content ?? item));
  } else add("Input", request.input, "user");

  if (Array.isArray(response)) {
    const thinking = response
      .map((event) => event.message?.thinking ?? event.choices?.[0]?.delta?.reasoning_content ?? (event.type === "response.reasoning_summary_text.delta" ? event.delta : ""))
      .join("");
    add("Reasoning", thinking, "reasoning");
    const text = response
      .map((event) =>
        event.type === "response.output_text.delta"
          ? event.delta
          : event.message?.content ?? event.choices?.[0]?.delta?.content ?? event.delta?.text ?? (typeof event.response === "string" ? event.response : ""),
      )
      .join("");
    if (text) add("Assistant", text, "assistant");
    for (const event of response) {
      if (event.message?.tool_calls) add("Tool call", event.message.tool_calls, "tool");
      if (event.type === "response.output_item.done" && event.item?.type === "function_call") add("Tool call", event.item, "tool");
      for (const choice of event.choices ?? []) if (choice.delta?.tool_calls) add("Tool call", choice.delta.tool_calls, "tool");
    }
    if (!text && !entries.some((entry) => entry.kind === "tool")) add("Response events", response, "other");
  } else if (response) {
    const message = response.choices?.[0]?.message ?? response.message;
    if (message?.reasoning_content || message?.thinking) add("Reasoning", message.reasoning_content ?? message.thinking, "reasoning");
    add("Assistant", flattenText(message?.content ?? response.content ?? response.output ?? response.response ?? response), "assistant");
    if (message?.tool_calls) add("Tool calls", message.tool_calls, "tool");
  }
  return entries;
}

function flattenText(content: unknown): unknown {
  if (!Array.isArray(content)) return content;
  if (content.every((part) => part && typeof part === "object" && "text" in part)) {
    return content.map((part) => (part as { text: string }).text).join("\n");
  }
  return content;
}

function kindOf(role: string): Entry["kind"] {
  const value = role.toLowerCase();
  if (value === "system" || value === "developer") return "system";
  if (value === "user") return "user";
  if (value === "assistant") return "assistant";
  if (value === "tool") return "tool";
  return "other";
}

const titleCase = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
