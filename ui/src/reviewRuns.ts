import { useSyncExternalStore } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { ApiError, api, askModel, followReply } from "./api";

/**
 * The AI reviews under way, kept outside any component: a review goes on when its Review tab is left
 * (to open its session, or any other page of the console) and is found again on coming back. Only
 * closing or reloading the console stops one, since the reply is read by this page.
 */
export interface ReviewRunState {
  /** The review's own session; "" until the server has opened it. */
  id: string;
  title: string;
  model: string;
  text: string;
  reasoning: string;
  /** starting: opening its session. waiting: sent, nothing back yet. Then thinking, writing, and how it ended. */
  phase: "starting" | "waiting" | "thinking" | "writing" | "done" | "stopped" | "failed";
  startedAt: number;
  endedAt?: number;
  failed?: string;
}

export const reviewBusy = (run: ReviewRunState | undefined) =>
  Boolean(run && (run.phase === "starting" || run.phase === "waiting" || run.phase === "thinking" || run.phase === "writing"));

const runs = new Map<string, ReviewRunState>();
const controllers = new Map<string, AbortController>();
const listeners = new Set<() => void>();

function update(reviewed: string, change: (run: ReviewRunState) => ReviewRunState): void {
  const run = runs.get(reviewed);
  if (!run) return;
  runs.set(reviewed, change(run));
  listeners.forEach((listener) => listener());
}

const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

/** The review of this session started from this page, if any: under way, or the last one finished. */
export const useReviewRun = (reviewed: string) => useSyncExternalStore(subscribe, () => runs.get(reviewed));

/** Only where the run stands and which session it is, for parts of the page that needn't redraw with every word. */
export const useReviewPhase = (reviewed: string) => useSyncExternalStore(subscribe, () => runs.get(reviewed)?.phase);
export const useReviewRunId = (reviewed: string) => useSyncExternalStore(subscribe, () => runs.get(reviewed)?.id);

let watching = false;
/** A page that closes can't read the reply any more: end the reviews it was running, so none is left open. */
function watchPageClose(): void {
  if (watching) return;
  watching = true;
  window.addEventListener("pagehide", () => {
    for (const [reviewed, controller] of controllers) {
      controller.abort();
      const id = runs.get(reviewed)?.id;
      if (id) api.endSession(id, { keepalive: true }).catch(() => undefined);
    }
  });
}

export async function startReviewRun(
  reviewed: string,
  choice: { backend: string; model: string },
  prompt: { id: string; title: string },
  question: string,
  client: QueryClient,
  notify: (message: string) => void,
): Promise<void> {
  if (reviewBusy(runs.get(reviewed))) return;
  watchPageClose();
  const controller = new AbortController();
  controllers.set(reviewed, controller);
  runs.set(reviewed, { id: "", title: prompt.title, model: choice.model, text: "", reasoning: "", phase: "starting", startedAt: Date.now() });
  listeners.forEach((listener) => listener());
  const append = (piece: { content?: string; reasoning?: string }) =>
    update(reviewed, (run) => ({
      ...run,
      text: run.text + (piece.content ?? ""),
      reasoning: run.reasoning + (piece.reasoning ?? ""),
      phase: piece.content || run.text ? "writing" : "thinking",
    }));
  let opened: string | null = null;
  try {
    const made = await api.startReview(reviewed, { ...choice, prompt: prompt.id, question: question.trim() || undefined });
    opened = made.session.session_id;
    const id = opened;
    update(reviewed, (run) => ({ ...run, id, model: made.model, phase: "waiting" }));
    // Its session shows on the Review tab's list and on Sessions from now, not only once it's finished.
    client.invalidateQueries({ queryKey: ["session-review", reviewed] });
    let requestId: string | null = null;
    try {
      await askModel(id, { model: made.model, messages: made.messages }, append, controller.signal, (started) => (requestId = started));
    } catch (error) {
      // The connection dropped mid-reply: the server went on reading it, so pick the rest up from the live view.
      if (controller.signal.aborted || !requestId || error instanceof ApiError) throw error;
      await followReply(
        id,
        requestId,
        (soFar) => update(reviewed, (run) => (soFar.content.length < run.text.length ? run : { ...run, text: soFar.content, reasoning: soFar.reasoning || run.reasoning })),
        append,
        controller.signal,
      ).catch(() => Promise.reject(error));
    }
    update(reviewed, (run) => ({ ...run, phase: "done", endedAt: Date.now() }));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const stopped = controller.signal.aborted;
    update(reviewed, (run) => ({ ...run, phase: stopped ? "stopped" : "failed", failed: stopped ? undefined : message, endedAt: Date.now() }));
    if (!stopped) notify(message);
  } finally {
    controllers.delete(reviewed);
    if (opened) await api.endSession(opened).catch(() => undefined);
    client.invalidateQueries({ queryKey: ["session-review", reviewed] });
    client.invalidateQueries({ queryKey: ["sessions"] });
  }
}

/** Stop: this page stops reading, and the server is told to stop reading the reply too. */
export function stopReviewRun(reviewed: string): void {
  controllers.get(reviewed)?.abort();
  const id = runs.get(reviewed)?.id;
  if (id) api.stopReplies(id).catch(() => undefined);
}
