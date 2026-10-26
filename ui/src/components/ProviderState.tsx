import type { BackendModels } from "../api";

// live (working) and ask (waiting for its user) are for running agents.
export type ProviderTone = "good" | "bad" | "idle" | "live" | "ask";

/** How a configured backend is doing, from its entry in /api/models. Hosted APIs aren't probed. */
export function providerState(type: string | undefined, models: BackendModels | undefined, pending: boolean): { tone: ProviderTone; text: string } {
  if (type === "anthropic") return { tone: "idle", text: "Pass-through" };
  if (!models) return { tone: "idle", text: pending ? "Checking…" : "Unknown" };
  if (!models.online) return { tone: "bad", text: "Offline" };
  const count = models.models.length;
  const loaded = models.models.filter((model) => model.loaded).length;
  if (!count) return { tone: "good", text: "Online, no models yet" };
  return { tone: "good", text: `${count} ${count === 1 ? "model" : "models"}${loaded ? `, ${loaded} loaded` : ""}` };
}

export function StateDot({ tone }: { tone: ProviderTone }) {
  return <span className={`state-dot ${tone}`} aria-hidden="true" />;
}
