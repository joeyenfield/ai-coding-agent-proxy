import { useEffect, useRef, useState } from "react";
import type { RequestRecord, TrafficKind } from "./api";

export type DeltaKind = "reasoning" | "content" | "tool";

export interface LiveSummary {
  messages: number | null;
  tools: number;
  tool_names: string[];
  system_chars: number;
  stream: boolean;
  ollama_model: string | null;
  num_ctx: number | null;
  num_predict: number | null;
  temperature: number | null;
  top_p: number | null;
  top_k: number | null;
  think: boolean | string | null;
  keep_alive: string | number | null;
  translated: boolean;
}

export interface LiveRequest {
  key: string;
  session_id: string;
  request_id: string;
  client: string;
  model: string;
  backend: string;
  endpoint: string;
  kind?: TrafficKind;
  method?: string | null;
  host?: string | null;
  started_at: number;
  first_token_at: number | null;
  reasoning: string;
  content: string;
  tool: string;
  chunks: Record<DeltaKind, number>;
  summary: LiveSummary | null;
  done: boolean;
  ended_at?: number;
  record?: RequestRecord;
  /** Client-side arrival times of recent chunks, for a rolling tokens/s figure. */
  arrivals: number[];
  lastKind: DeltaKind | null;
}

export interface LiveState {
  connected: boolean;
  enabled: boolean;
  active: LiveRequest[];
  recent: LiveRequest[];
}

type Snapshot = { enabled: boolean; active: LiveRequest[]; recent: LiveRequest[] };
type Delta = { key: string; kind: DeltaKind; text: string };

// Matches the proxy's tail; account agents make many small non-model requests.
const RECENT_LIMIT = 40;
const RATE_WINDOW_MS = 3000;

const hydrate = (entry: LiveRequest): LiveRequest => ({ ...entry, arrivals: [], lastKind: entry.lastKind ?? null });

/** Subscribe to /api/live. Token deltas are applied once per animation frame. */
export function useLive(sessionId?: string): LiveState {
  const [state, setState] = useState<LiveState>({ connected: false, enabled: true, active: [], recent: [] });
  const store = useRef<{ active: Map<string, LiveRequest>; recent: LiveRequest[]; enabled: boolean }>({
    active: new Map(),
    recent: [],
    enabled: true,
  });
  const frame = useRef<number | null>(null);

  useEffect(() => {
    const source = new EventSource(sessionId ? `/api/live?session_id=${encodeURIComponent(sessionId)}` : "/api/live");
    const publish = (connected = true) => {
      if (frame.current != null) return;
      frame.current = requestAnimationFrame(() => {
        frame.current = null;
        const { active, recent, enabled } = store.current;
        setState({ connected, enabled, active: [...active.values()], recent: [...recent] });
      });
    };
    source.addEventListener("open", () => publish(true));
    source.addEventListener("error", () => setState((current) => ({ ...current, connected: false })));
    source.addEventListener("snapshot", (event) => {
      const data = JSON.parse((event as MessageEvent).data) as Snapshot;
      store.current = {
        enabled: data.enabled,
        active: new Map(data.active.map((entry) => [entry.key, hydrate(entry)])),
        recent: data.recent.map(hydrate),
      };
      publish();
    });
    source.addEventListener("start", (event) => {
      const { request } = JSON.parse((event as MessageEvent).data) as { request: LiveRequest };
      store.current.active.set(request.key, hydrate(request));
      publish();
    });
    source.addEventListener("summary", (event) => {
      const { key, summary } = JSON.parse((event as MessageEvent).data) as { key: string; summary: LiveSummary };
      const entry = store.current.active.get(key);
      if (!entry) return;
      store.current.active.set(key, { ...entry, summary });
      publish();
    });
    source.addEventListener("delta", (event) => {
      const delta = JSON.parse((event as MessageEvent).data) as Delta;
      const entry = store.current.active.get(delta.key);
      if (!entry) return;
      const now = Date.now();
      const arrivals = entry.arrivals.filter((time) => now - time < RATE_WINDOW_MS);
      arrivals.push(now);
      store.current.active.set(delta.key, {
        ...entry,
        [delta.kind]: entry[delta.kind] + delta.text,
        chunks: { ...entry.chunks, [delta.kind]: entry.chunks[delta.kind] + 1 },
        first_token_at: entry.first_token_at ?? now / 1000,
        arrivals,
        lastKind: delta.kind,
      });
      publish();
    });
    source.addEventListener("end", (event) => {
      const { key, record } = JSON.parse((event as MessageEvent).data) as { key: string; record: RequestRecord };
      const entry = store.current.active.get(key);
      store.current.active.delete(key);
      if (entry) {
        store.current.recent = [{ ...entry, done: true, ended_at: Date.now() / 1000, record }, ...store.current.recent].slice(0, RECENT_LIMIT);
      }
      publish();
    });
    return () => {
      source.close();
      if (frame.current != null) cancelAnimationFrame(frame.current);
      frame.current = null;
    };
  }, [sessionId]);

  return state;
}

/** Approximate tokens per second over the last few seconds; Ollama streams about one token per chunk. */
export function liveRate(entry: LiveRequest): number | null {
  const now = Date.now();
  const recent = entry.arrivals.filter((time) => now - time < RATE_WINDOW_MS);
  if (recent.length < 2) return null;
  const span = Math.max(now - recent[0], 500);
  return (recent.length * 1000) / span;
}

export type Phase = "waiting" | "thinking" | "answering" | "tools" | "done";

export function phaseOf(entry: LiveRequest): Phase {
  if (entry.done) return "done";
  if (!entry.lastKind) return "waiting";
  return entry.lastKind === "reasoning" ? "thinking" : entry.lastKind === "tool" ? "tools" : "answering";
}

/** Re-render on an interval while something is live, so elapsed times and rates keep moving. */
export function useTicker(active: boolean, interval = 500) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((value) => value + 1), interval);
    return () => window.clearInterval(timer);
  }, [active, interval]);
}
