import { useEffect, useRef, useState } from "react";
import { apiUrl, LIVE_RETRY_MS, onRenewed, renewSignIn, type RequestRecord, type TrafficKind } from "./api";

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
// How long a live stream may be reconnecting before the page says it's disconnected.
const LIVE_GAP_MS = 4000;

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
    const url = apiUrl(sessionId ? `/api/live?session_id=${encodeURIComponent(sessionId)}` : "/api/live");
    let source: EventSource | null = null;
    // A stream opened on a renewed cookie, waiting for its snapshot to replace source.
    let pending: EventSource | null = null;
    let stopped = false;
    let attempt = 0;
    let retry: number | undefined;
    // When the current stream was opened: a renewal made since (by the console's timer, or another tab) counts.
    let opened = 0;
    // The server ends a stream when the access token it opened with runs out (every few minutes), and
    // the browser reopens it a second later on the renewed cookie. Only a gap longer than this shows.
    let lost: number | undefined;
    // EventSource reconnects by itself after a dropped connection, but gives up for good when a
    // reconnect is refused (401 once the access token expired, 502 or 503 during a restart). Then
    // renew the sign-in and open a new one, backing off; each new one starts with a snapshot.
    const connect = () => {
      window.clearTimeout(retry);
      retry = undefined;
      if (stopped) return;
      source?.close();
      pending?.close();
      pending = null;
      opened = Date.now();
      source = new EventSource(url, { withCredentials: true });
      listen(source);
    };
    const reconnect = async () => {
      const delay = LIVE_RETRY_MS[Math.min(attempt, LIVE_RETRY_MS.length - 1)];
      attempt += 1;
      await renewSignIn(opened);
      if (stopped || retry !== undefined || source?.readyState !== EventSource.CLOSED) return;
      retry = window.setTimeout(connect, delay);
    };
    // Coming back online or to the page needn't wait for the backoff.
    const wake = () => {
      if (stopped || document.visibilityState !== "visible" || source?.readyState !== EventSource.CLOSED) return;
      void renewSignIn(opened).then(() => {
        if (source?.readyState === EventSource.CLOSED) connect();
      });
    };
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    // The server ends a stream when the token it opened with runs out. After each renewal, open the
    // next one on the new cookie ahead of that, so the page never sees a gap.
    const unrenewed = onRenewed(() => {
      if (stopped || pending || source?.readyState !== EventSource.OPEN) return;
      pending = new EventSource(url, { withCredentials: true });
      listen(pending);
    });
    const publish = (connected = true) => {
      if (frame.current != null) return;
      frame.current = requestAnimationFrame(() => {
        frame.current = null;
        const { active, recent, enabled } = store.current;
        setState({ connected, enabled, active: [...active.values()], recent: [...recent] });
      });
    };
    function listen(stream: EventSource) {
      // Only the current stream's events count: a stream opened after a renewal (pending) takes over
      // with its snapshot, and the one it replaces is closed then.
      const mine = <E extends Event>(handler: (event: E) => void) => (event: E) => {
        if (stream === source) handler(event);
      };
      stream.addEventListener("open", mine(() => {
        attempt = 0;
        opened = Date.now();
        window.clearTimeout(lost);
        lost = undefined;
        publish(true);
      }));
      stream.addEventListener("error", () => {
        if (stream === pending) {
          // The new stream didn't open: keep the old one, which reconnects as before when it ends.
          stream.close();
          pending = null;
          return;
        }
        if (stream !== source) return;
        if (lost === undefined) {
          lost = window.setTimeout(() => {
            lost = undefined;
            if (!stopped && source?.readyState !== EventSource.OPEN) setState((current) => ({ ...current, connected: false }));
          }, LIVE_GAP_MS);
        }
        if (stream.readyState === EventSource.CLOSED) void reconnect();
      });
      stream.addEventListener("snapshot", (event) => {
        if (stream === pending) {
          source?.close();
          source = stream;
          pending = null;
          opened = Date.now();
        }
        if (stream !== source) return;
        const data = JSON.parse((event as MessageEvent).data) as Snapshot;
        store.current = {
          enabled: data.enabled,
          active: new Map(data.active.map((entry) => [entry.key, hydrate(entry)])),
          recent: data.recent.map(hydrate),
        };
        publish();
      });
      stream.addEventListener("start", mine((event) => {
        const { request } = JSON.parse((event as MessageEvent).data) as { request: LiveRequest };
        store.current.active.set(request.key, hydrate(request));
        publish();
      }));
      stream.addEventListener("summary", mine((event) => {
        const { key, summary } = JSON.parse((event as MessageEvent).data) as { key: string; summary: LiveSummary };
        const entry = store.current.active.get(key);
        if (!entry) return;
        store.current.active.set(key, { ...entry, summary });
        publish();
      }));
      stream.addEventListener("delta", mine((event) => {
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
      }));
      stream.addEventListener("end", mine((event) => {
        const { key, record } = JSON.parse((event as MessageEvent).data) as { key: string; record: RequestRecord };
        const entry = store.current.active.get(key);
        store.current.active.delete(key);
        if (entry) {
          store.current.recent = [{ ...entry, done: true, ended_at: Date.now() / 1000, record }, ...store.current.recent].slice(0, RECENT_LIMIT);
        }
        publish();
      }));
    }
    connect();
    return () => {
      stopped = true;
      window.clearTimeout(retry);
      window.clearTimeout(lost);
      unrenewed();
      pending?.close();
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
      source?.close();
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
