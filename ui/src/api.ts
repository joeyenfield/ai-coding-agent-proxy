export interface Backend {
  type: string;
  url: string;
  metrics_url?: string;
}

export interface ActiveRequest {
  session_id: string;
  request_id: string;
  client: string;
  model: string;
  backend: string;
  endpoint: string;
  timestamp: string;
  response_bytes: number;
  ttft_ms: number | null;
  output_tokens: number;
}

export interface RequestRecord {
  request_id: string;
  timestamp: string;
  session_id: string;
  client: string;
  model: string;
  backend: string;
  endpoint: string;
  request_bytes: number;
  response_bytes: number;
  input_tokens: number;
  output_tokens: number;
  ttft_ms: number | null;
  prompt_eval_ms: number | null;
  generation_ms: number | null;
  total_ms: number | null;
  prompt_tps: number | null;
  generation_tps: number | null;
  status: number;
  error_type: string | null;
  context_size: number | null;
  temperature: number | null;
  max_output_tokens: number | null;
  trace_available?: boolean;
}

export interface Status {
  proxy_instance_id: string;
  proxy_url: string;
  listen_address: string;
  restart_required: boolean;
  default_backend: string;
  backends: Record<string, Backend>;
  active_sessions: number;
  total_sessions: number;
  total_requests: number;
  total_input_tokens: number;
  total_output_tokens: number;
  current_requests: ActiveRequest[];
  recent_requests: RequestRecord[];
}

export interface Session {
  session_id: string;
  client: string;
  project: string | null;
  model: string | null;
  backend: string;
  trace: boolean;
  started_at: string;
  ended_at: string | null;
  exit_status: number | null;
  tags: Record<string, unknown>;
  request_count: number;
  last_activity_at: string | null;
}

export interface Trace {
  metadata: Record<string, unknown>;
  request: unknown;
  response: unknown;
  upstream_request?: unknown;
}

export interface Agent {
  id: string;
  name: string;
  executable: string;
  protocol: "openai-chat" | "openai-responses" | "anthropic" | "ollama";
  install: string | null;
  homepage: string | null;
  description: string | null;
  recommended_for: string[];
  notes: string | null;
  installed: boolean;
}

export interface PreparedSession {
  session: Session;
  env: Record<string, string>;
  command: string[];
  shell: { bash: string; powershell: string };
}

export interface Profile {
  ollama_model: string;
  options: Record<string, unknown>;
  keep_alive: string | number | null;
  think: boolean | string | null;
  translate_openai: boolean;
  matched: string[];
}

export interface OllamaModel {
  name: string;
  size: number | null;
  modified_at: string | null;
  family: string | null;
  parameter_size: string | null;
  quantization: string | null;
  loaded: boolean;
  context_length: number | null;
  size_loaded: number | null;
  size_vram: number | null;
  profile: Profile;
}

export interface BackendModels {
  online: boolean;
  url?: string;
  version?: string;
  error?: string;
  models: OllamaModel[];
}

export interface ModelsResponse {
  backends: Record<string, BackendModels>;
  aliases: Record<string, Profile>;
}

export interface NetworkConfig {
  proxy: { listen_host: string; listen_port: number; url: string };
  default_backend: string;
  backends: Record<string, Backend>;
}

export interface ConfigResponse {
  config: NetworkConfig;
  restart_required: boolean;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: init?.body ? { "content-type": "application/json", ...init.headers } : init?.headers,
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (body?.detail) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // Keep the status-based message.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

const sessionUrl = (id: string) => `/api/sessions/${encodeURIComponent(id)}`;

export const api = {
  status: () => request<Status>("/api/status"),
  sessions: () => request<Session[]>("/api/sessions"),
  session: (sessionId: string) => request<Session>(sessionUrl(sessionId)),
  requests: () => request<RequestRecord[]>("/api/requests"),
  trace: (sessionId: string, requestId: string) =>
    request<Trace>(`${sessionUrl(sessionId)}/traces/${encodeURIComponent(requestId)}`),
  createSession: (body: { client: string; model: string | null; backend: string; trace: boolean }) =>
    request<Session>("/api/sessions", { method: "POST", body: JSON.stringify(body) }),
  setTrace: (sessionId: string, trace: boolean) =>
    request<Session>(sessionUrl(sessionId), { method: "PATCH", body: JSON.stringify({ ended: false, trace }) }),
  endSession: (sessionId: string) =>
    request<Session>(sessionUrl(sessionId), { method: "PATCH", body: JSON.stringify({ ended: true }) }),
  clearSession: (sessionId: string) => request<Session>(`${sessionUrl(sessionId)}/data`, { method: "DELETE" }),
  deleteSession: (sessionId: string) => request<{ deleted: string }>(sessionUrl(sessionId), { method: "DELETE" }),
  telemetryUrl: (sessionId: string) => `${sessionUrl(sessionId)}/telemetry`,
  agents: () => request<Agent[]>("/api/agents"),
  prepareAgent: (agentId: string, body: { model: string; backend: string; trace: boolean; project?: string }) =>
    request<PreparedSession>(`/api/agents/${encodeURIComponent(agentId)}/sessions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  models: () => request<ModelsResponse>("/api/models"),
  config: () => request<ConfigResponse>("/api/config"),
  saveConfig: (config: NetworkConfig) =>
    request<ConfigResponse>("/api/config", { method: "PUT", body: JSON.stringify(config) }),
};

export const requestKey = (record: { session_id: string; request_id: string }) =>
  `${record.session_id}:${record.request_id}`;

export const isFailure = (record: RequestRecord) => record.status >= 400 || Boolean(record.error_type);

export interface GpuSample {
  index: number;
  name: string;
  utilization: number | null;
  memory_used: number | null;
  memory_total: number | null;
  temperature: number | null;
  power_watts: number | null;
}

export interface MachineSample {
  hostname: string;
  platform: string;
  timestamp: number;
  cpu: { percent: number; count: number };
  memory: { used: number; total: number; percent: number };
  gpus: GpuSample[];
  gpu_error: string | null;
}

export interface RunningModel {
  name: string;
  size: number;
  size_vram: number;
  gpu_percent: number | null;
  context_length: number | null;
  expires_at: string | null;
  parameter_size: string | null;
  quantization: string | null;
}

export interface HostState {
  url: string;
  local: boolean;
  online: boolean;
  version?: string;
  error?: string;
  models: RunningModel[];
  machine: { source: "local" | "remote" | null; data?: MachineSample; error?: string };
  traffic: { in_flight: number; recent_requests: number; average_tps: number | null };
}

export interface HostSample extends HostState {
  name: string;
  sampled_at: number;
}

export const fetchHost = (name: string) =>
  fetch(`/api/hosts/${encodeURIComponent(name)}`).then((response) => {
    if (!response.ok) throw new ApiError(`Request failed (${response.status})`, response.status);
    return response.json() as Promise<HostSample>;
  });
