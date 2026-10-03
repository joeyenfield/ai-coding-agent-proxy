import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchLiveDetails, type LiveDetails as Details } from "../api";
import { formatBytes } from "../format";
import { download, useClipboard } from "../hooks";
import { Conversation } from "./Inspector";

type Tab = "conversation" | "request" | "upstream" | "raw" | "metadata";

const TAB_LABELS: Record<Tab, string> = {
  conversation: "Conversation",
  request: "Request",
  upstream: "Sent to Ollama",
  raw: "Raw stream",
  metadata: "Metadata",
};

/** Payloads and raw chunks for one live request, fetched only while this panel is open. */
export function LiveDetails({ sessionId, requestId, done }: { sessionId: string; requestId: string; done: boolean }) {
  const [tab, setTab] = useState<Tab>("conversation");
  const [wrap, setWrap] = useState(true);
  const copy = useClipboard();
  const details = useQuery({
    queryKey: ["live-details", sessionId, requestId],
    queryFn: () => fetchLiveDetails(sessionId, requestId),
    // Keep the raw stream moving while the request runs; one final fetch after it ends.
    refetchInterval: done ? false : 1500,
    gcTime: 0,
  });
  const data = details.data;
  const tabs: Tab[] = ["conversation", "request", ...(data?.upstream ? (["upstream"] as Tab[]) : []), "raw", "metadata"];
  const text = useMemo(() => (data && tab !== "conversation" ? render(data, tab) : ""), [data, tab]);

  if (details.isPending) return <p className="subtle">Loading the request…</p>;
  if (details.isError) return <p className="bad">{details.error.message}</p>;
  if (!data) return null;
  if (!data.enabled) {
    return <p className="subtle">Payloads aren't kept because live text is turned off on the proxy (AI_PROXY_LIVE=0).</p>;
  }
  const filename = `${sessionId}-${requestId}-${tab}.json`;
  return (
    <div className="live-details">
      <div className="tabs" role="tablist" aria-label="Request details">
        {tabs.map((name) => (
          <button key={name} type="button" role="tab" aria-selected={tab === name} onClick={() => setTab(name)}>
            {TAB_LABELS[name]}
          </button>
        ))}
      </div>
      <div className="payload-bar">
        <span className="subtle">
          {tab === "conversation"
            ? "Readable view of the messages so far"
            : tab === "raw"
              ? `Latest ${data.raw.length} of ${data.raw_total.toLocaleString()} chunks from Ollama${done ? "" : ", updating"}`
              : formatBytes(new TextEncoder().encode(text).length)}
        </span>
        {tab !== "conversation" && (
          <div className="row">
            <label className="check">
              <input type="checkbox" checked={wrap} onChange={(event) => setWrap(event.target.checked)} />
              Wrap
            </label>
            <button type="button" onClick={() => copy(text, "Copied")}>
              Copy
            </button>
            <button type="button" onClick={() => download(text, filename)}>
              Download
            </button>
          </div>
        )}
      </div>
      <div className="payload-area live-payload">
        {tab === "conversation" ? (
          // The raw buffer only holds the latest chunks, so use the full accumulated text instead.
          <Conversation
            trace={{
              metadata: {},
              request: data.request,
              response: [{ message: { thinking: data.reasoning, content: data.content } }],
            }}
          />
        ) : (
          <pre className={`code ${wrap ? "wrap" : ""}`}>{text}</pre>
        )}
      </div>
    </div>
  );
}

function render(data: Details, tab: Tab): string {
  if (tab === "raw") return data.raw.map((chunk) => JSON.stringify(chunk)).join("\n");
  if (tab === "request") return JSON.stringify(data.request, null, 2);
  if (tab === "upstream") return JSON.stringify(data.upstream, null, 2);
  const { request: _request, upstream: _upstream, raw: _raw, reasoning: _reasoning, content: _content, tool: _tool, ...metadata } = data;
  return JSON.stringify(metadata, null, 2);
}
