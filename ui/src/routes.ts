import type { Backend, TrafficKind } from "./api";

/**
 * The three ways traffic reaches a model through this proxy.
 *
 * ollama:  local models: Ollama machines (translated and tuned by the proxy), Strata and Docker Model Runner (passed through).
 * hosted:  a hosted API backend (Anthropic) reached through a proxy endpoint, passed through unchanged.
 * account: an agent signed in to its own service; its HTTPS is captured by the intercept listener.
 */
export type RouteKind = "ollama" | "hosted" | "account";

export const ROUTE_ORDER: RouteKind[] = ["account", "ollama", "hosted"];

export const ROUTES: Record<RouteKind, { label: string; description: string }> = {
  ollama: {
    label: "Local models",
    description: "Your Ollama machines, the Strata server and Docker Model Runner. For Ollama the proxy translates each request and applies the model settings from models.yaml; Strata and Docker take requests as they are.",
  },
  hosted: {
    label: "Hosted API",
    description: "A hosted API backend such as Anthropic. Requests pass through unchanged and use the agent's own credentials.",
  },
  account: {
    label: "Your accounts",
    description: "Agents signed in to Claude, GitHub Copilot or ChatGPT. They talk to their own service and everything they send is captured in transit.",
  },
};

/** What the console calls each backend type; short names go in badges. */
export const PROVIDERS: Record<string, { label: string; short: string }> = {
  ollama: { label: "Ollama", short: "Ollama" },
  strata: { label: "Strata", short: "Strata" },
  docker: { label: "Docker Model Runner", short: "Docker" },
  anthropic: { label: "Anthropic API", short: "Hosted API" },
};

export const providerLabel = (type: string | undefined, short = false) => {
  const provider = PROVIDERS[type ?? ""];
  return provider ? provider[short ? "short" : "label"] : type ?? "Backend";
};

/** Session backend name the proxy uses for account agents. */
export const DIRECT_BACKEND = "direct";

const ACCOUNT_NAMES: Record<string, string> = {
  "claude-account": "Claude account",
  "copilot-account": "GitHub Copilot account",
  "codex-account": "ChatGPT account",
  unattributed: "Unattributed",
};

interface Routed {
  backend: string;
  client?: string;
  kind?: TrafficKind;
}

export function routeOf(item: Routed, backends: Record<string, Backend> | undefined): RouteKind {
  if ((item.kind && item.kind !== "api") || item.backend === DIRECT_BACKEND) return "account";
  return backends?.[item.backend]?.type === "anthropic" ? "hosted" : "ollama";
}

/** Short text naming where one request or session went, e.g. "Ollama · desktop". */
export function routeDetail(item: Routed, route: RouteKind, backends?: Record<string, Backend>): string {
  if (route === "account") return ACCOUNT_NAMES[item.client ?? ""] ?? "Account";
  if (route === "hosted") return `Anthropic API · ${item.backend}`;
  return `${providerLabel(backends?.[item.backend]?.type ?? "ollama", true)} · ${item.backend}`;
}
