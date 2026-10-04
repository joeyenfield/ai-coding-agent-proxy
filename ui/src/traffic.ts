/** Plain-language labels for the hosts and endpoints coding agents talk to, and list rows built from them. */

import { requestKey, type RequestRecord } from "./api";
import { endpointPath } from "./format";
import type { LiveRequest } from "./live";

export type Category = "model" | "tokens" | "models" | "account" | "signin" | "mcp" | "telemetry" | "config" | "updates" | "connection" | "other";

export const CATEGORY_LABELS: Record<Category, string> = {
  model: "Model call",
  tokens: "Token count",
  models: "Model list",
  account: "Account",
  signin: "Sign-in",
  mcp: "MCP",
  telemetry: "Telemetry",
  config: "Settings & flags",
  updates: "Updates",
  connection: "Connection",
  other: "Other",
};

/** Display order for the summary; the busiest agent chatter sits after the interesting parts. */
export const CATEGORY_ORDER: Category[] = ["model", "tokens", "models", "mcp", "account", "signin", "config", "telemetry", "updates", "connection", "other"];

export interface Classified {
  /** Who runs the host, e.g. "Anthropic API". */
  service: string;
  category: Category;
}

interface Rule {
  host: RegExp;
  service: string;
  paths?: [RegExp, Category][];
  fallback: Category;
}

const RULES: Rule[] = [
  {
    host: /^api\.anthropic\.com$/,
    service: "Anthropic API",
    paths: [
      [/^\/v1\/messages\/count_tokens/, "tokens"],
      [/^\/v1\/messages/, "model"],
      [/^\/v1\/models/, "models"],
      [/^\/(v1\/mcp_servers|mcp-registry)/, "mcp"],
      [/^\/api\/oauth\/(token|authorize)/, "signin"],
      [/^\/api\/oauth\//, "account"],
      [/^\/api\/(event_logging|claude_code\/metrics)/, "telemetry"],
      [/^\/api\/(claude_cli|claude_code|claude_code_penguin_mode|eval)/, "config"],
    ],
    fallback: "other",
  },
  { host: /^mcp-proxy\.anthropic\.com$/, service: "Anthropic MCP proxy", fallback: "mcp" },
  { host: /^(console\.anthropic\.com|claude\.ai|platform\.claude\.com)$/, service: "Claude sign-in", fallback: "signin" },
  { host: /^statsig\.anthropic\.com$/, service: "Anthropic feature flags", fallback: "config" },
  { host: /(^|\.)(storage\.googleapis\.com|downloads\.claude\.ai)$/, service: "Claude Code downloads", fallback: "updates" },
  {
    host: /^api\.(individual\.|business\.|enterprise\.)?githubcopilot\.com$/,
    service: "GitHub Copilot API",
    paths: [
      [/^\/(v1\/messages|chat\/completions|responses|v1\/chat\/completions|v1\/responses)/, "model"],
      [/^\/models/, "models"],
      [/^\/mcp/, "mcp"],
    ],
    fallback: "other",
  },
  { host: /^(copilot-)?telemetry\.(.+\.)?githubcopilot\.com$/, service: "GitHub Copilot telemetry", fallback: "telemetry" },
  { host: /^origin-tracker\.githubusercontent\.com$/, service: "GitHub", fallback: "telemetry" },
  {
    host: /^api\.github\.com$/,
    service: "GitHub API",
    paths: [
      [/^\/copilot_internal\/v\d+\/token/, "signin"],
      [/^\/copilot_internal\//, "account"],
      [/^\/repos\/github\/copilot-cli\/releases/, "updates"],
      [/^\/user/, "account"],
    ],
    fallback: "other",
  },
  { host: /^github\.com$/, service: "GitHub sign-in", paths: [[/^\/login/, "signin"]], fallback: "other" },
  {
    host: /^chatgpt\.com$/,
    service: "ChatGPT (Codex)",
    paths: [
      [/^\/backend-api\/codex\/responses/, "model"],
      [/^\/backend-api\/codex\/models/, "models"],
      [/^\/backend-api\/codex\/analytics/, "telemetry"],
      [/^\/backend-api\/(ps\/mcp|ps\/plugins|plugins)/, "mcp"],
      [/^\/backend-api\/(wham|accounts|me)/, "account"],
    ],
    fallback: "other",
  },
  { host: /^ab\.chatgpt\.com$/, service: "ChatGPT telemetry", fallback: "telemetry" },
  { host: /^auth\.openai\.com$/, service: "OpenAI sign-in", fallback: "signin" },
  {
    host: /^api\.openai\.com$/,
    service: "OpenAI API",
    paths: [
      [/^\/v1\/(responses|chat\/completions)/, "model"],
      [/^\/v1\/models/, "models"],
    ],
    fallback: "other",
  },
  { host: /(^|\.)datadoghq\.(com|eu)$/, service: "Datadog", fallback: "telemetry" },
  { host: /(^|\.)sentry\.io$/, service: "Sentry", fallback: "telemetry" },
  { host: /(^|\.)registry\.npmjs\.org$/, service: "npm registry", fallback: "updates" },
];

const MODEL_PATHS = /\/(v1\/messages|chat\/completions|responses|api\/chat|api\/generate)$/;

export function classify(row: { kind: string; host: string; path: string; model: string }): Classified {
  if (row.kind === "api") {
    // Proxy endpoints: requests the proxy answers itself or forwards to a configured backend.
    const path = row.path;
    if (/count_tokens$/.test(path)) return { service: "Proxy", category: "tokens" };
    if (/\/(models|api\/tags)$/.test(path)) return { service: "Proxy", category: "models" };
    return { service: "Proxy", category: MODEL_PATHS.test(path) || row.model ? "model" : "other" };
  }
  if (row.kind === "tunnel" || row.kind === "upgrade") {
    const known = RULES.find((rule) => rule.host.test(row.host));
    return { service: known?.service ?? row.host, category: "connection" };
  }
  const rule = RULES.find((item) => item.host.test(row.host));
  if (!rule) return { service: row.host, category: row.model ? "model" : "other" };
  const match = rule.paths?.find(([pattern]) => pattern.test(row.path));
  return { service: rule.service, category: match?.[1] ?? (row.model ? "model" : rule.fallback) };
}

/** One line in the traffic list: a finished record, or a request still in flight. */
export interface Row {
  key: string;
  session_id: string;
  request_id: string;
  client: string;
  model: string;
  kind: string;
  method: string;
  host: string;
  path: string;
  started: number;
  service: string;
  category: Category;
  record?: RequestRecord;
  live?: LiveRequest;
  /** Older identical requests folded into this row (same session, method, host and path, back to back). */
  repeats?: Row[];
}

export function fromRecord(record: RequestRecord): Row {
  const intercepted = record.kind && record.kind !== "api";
  return {
    key: requestKey(record),
    session_id: record.session_id,
    request_id: record.request_id,
    client: record.client,
    model: record.model,
    kind: record.kind ?? "api",
    method: record.method ?? "POST",
    host: intercepted ? record.host ?? record.backend : `proxy → ${record.backend}`,
    path: intercepted ? record.endpoint : endpointPath(record.endpoint),
    started: new Date(record.timestamp).getTime(),
    ...classify({ kind: record.kind ?? "api", host: record.host ?? "", path: record.endpoint, model: record.model }),
    record,
  };
}

export function fromLive(entry: LiveRequest): Row {
  const intercepted = entry.kind && entry.kind !== "api";
  return {
    key: entry.key,
    session_id: entry.session_id,
    request_id: entry.request_id,
    client: entry.client,
    model: entry.model,
    kind: entry.kind ?? "api",
    method: entry.method ?? "POST",
    host: intercepted ? entry.host ?? entry.backend : `proxy → ${entry.backend}`,
    path: intercepted ? entry.endpoint : endpointPath(entry.endpoint),
    started: entry.started_at * 1000,
    ...classify({ kind: entry.kind ?? "api", host: entry.host ?? "", path: entry.endpoint, model: entry.model }),
    record: entry.record,
    live: entry,
  };
}

/** Fold back-to-back identical requests (agents poll some endpoints many times) into the newest one. */
export function groupRepeats(rows: Row[]): Row[] {
  const result: Row[] = [];
  for (const row of rows) {
    const previous = result[result.length - 1];
    if (
      previous &&
      previous.session_id === row.session_id &&
      previous.method === row.method &&
      previous.host === row.host &&
      previous.path === row.path &&
      previous.category !== "model" &&
      Boolean(previous.record) === Boolean(row.record)
    ) {
      previous.repeats = [...(previous.repeats ?? []), row];
      continue;
    }
    result.push({ ...row, repeats: undefined });
  }
  return result;
}

/** Finished records plus in-flight and recently finished live requests, newest first. */
export function mergeRows(records: RequestRecord[], live: LiveRequest[]): Row[] {
  const byKey = new Map<string, Row>();
  for (const record of records) byKey.set(requestKey(record), fromRecord(record));
  for (const entry of live) {
    const existing = byKey.get(entry.key);
    if (existing) existing.live = entry;
    else byKey.set(entry.key, fromLive(entry));
  }
  return [...byKey.values()].sort((a, b) => b.started - a.started);
}
