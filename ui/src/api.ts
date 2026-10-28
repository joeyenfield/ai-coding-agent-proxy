export interface Backend {
  type: string;
  url: string;
  /** The fleet machine that runs it. Hosted APIs have none, and so do backends set up before machines supplied them. */
  machine?: string;
  /** The model server on its machine, such as ollama-11434, in /api/status; the console manages it through the machine. */
  provider?: string;
  /** Its machine's name, in /api/status; null when the machine isn't in the fleet. */
  machine_name?: string | null;
  /** Set when its machine runs this backend in a container. */
  container?: ContainerSpec;
}

/** Local model servers run on machines; only hosted APIs don't. */
export const MACHINE_TYPES = ["ollama", "strata", "docker"];

export interface ContainerSpec {
  engine: string;
  image: string;
  port: number;
  /** all, none, or card numbers such as 0 or 0,1. */
  gpus: string;
  env: Record<string, string>;
  /** Model storage of its own instead of the engine's shared one. */
  volume?: string;
}

export interface EngineOption {
  env: string;
  label: string;
  choices: string[];
  hint: string;
}

export interface ContainerEngine {
  id: string;
  label: string;
  backend_type: string;
  image: string;
  images: string[];
  port: number;
  data_path: string;
  volume: string;
  health_path: string;
  stop_timeout: number;
  options: EngineOption[];
  summary: string;
}

/** Whether a machine runs containers for the server, and what it can run (from the machine itself). */
export interface MachineContainers {
  enabled: boolean;
  cli: string;
  found: boolean;
  online: boolean;
  version: string | null;
  error: string | null;
  engines: ContainerEngine[];
}

/** What a managed container is doing: not_created, pulling, starting, loading, ready, unhealthy, stopping, stopped or failed. */
export type ContainerPhase = "not_created" | "pulling" | "starting" | "loading" | "ready" | "unhealthy" | "stopping" | "stopped" | "failed";

export interface ContainerJob {
  action: "start" | "switch" | "stop" | "restart";
  state: "running" | "done" | "failed";
  started_at: number;
  finished_at: number | null;
  error: string | null;
  lines: string[];
}

export interface ContainerStatus {
  name: string;
  engine: string;
  engine_label: string;
  backend_type: string;
  url: string;
  spec: ContainerSpec;
  container: string;
  state: { status: string; running: boolean; health: string | null; started_at: string | null; exit_code: number | null; error: string | null } | null;
  ready: boolean;
  phase: ContainerPhase;
  /** Its settings changed since the container was created; the next start replaces it. */
  outdated: boolean;
  job: ContainerJob | null;
  command: string | null;
  problem: string | null;
  /** Running containers using the GPUs this one needs; starting it switches them off. */
  conflicts: string[];
  volume: string | null;
  data_path: string | null;
  default: boolean;
  /** The machine that runs it; machine_name is null once that machine is forgotten. */
  machine: string;
  machine_name: string | null;
  /** Connected, with containers on: it can be started, stopped and read now. */
  machine_online: boolean;
}

/** A machine as the Containers page sees it: whether it runs containers, and which engines. */
export interface ContainerHost {
  id: string;
  name: string;
  owner: string | null;
  connected: boolean;
  /** null until the machine has said (an older client doesn't). */
  enabled: boolean | null;
  cli: string | null;
  found: boolean | null;
  online: boolean | null;
  version: string | null;
  error: string | null;
  engines: ContainerEngine[];
  /** Whether you may create containers on it: it's yours. */
  you: { owner: boolean };
}

export interface ContainersOverview {
  machines: ContainerHost[];
  containers: ContainerStatus[];
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
  kind?: TrafficKind;
  method?: string | null;
  host?: string | null;
}

/** api: a proxy endpoint. intercept/upgrade/tunnel: captured by the HTTPS intercept proxy. */
export type TrafficKind = "api" | "intercept" | "upgrade" | "tunnel";

export interface HttpExchange {
  method: string;
  url: string;
  request_headers: [string, string][];
  request_content_type?: string | null;
  status?: number;
  reason?: string;
  response_headers?: [string, string][];
  response_content_type?: string;
}

export interface InterceptInfo {
  enabled: boolean;
  running: boolean;
  error: string | null;
  /** The agent-proxy's HTTPS proxy on each agent machine, which tunnels to this server. */
  url: string;
  listen_host: string;
  /** Set only when this server also accepts agents directly (tests, the demo). */
  listen_port: number | null;
  passthrough: string[];
  ca_path?: string;
  ca_fingerprint?: string;
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
  /** Set when the stream ended before Ollama reported counts; counts were estimated. */
  tokens_estimated?: boolean;
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
  kind?: TrafficKind;
  method?: string | null;
  host?: string | null;
  cache_read_tokens?: number;
  cache_creation_tokens?: number;
}

export interface Status {
  proxy_instance_id: string;
  proxy_url: string;
  listen_address: string;
  configured_listen_address: string;
  restart_required: boolean;
  started_at: number;
  /** Whether the console can restart the proxy (agent-server runs it), and whether a restart is under way. */
  restart?: { available: boolean; pending: boolean; address_pinned: boolean };
  default_backend: string;
  backends: Record<string, Backend>;
  intercept?: InterceptInfo;
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
  /** The fleet machine it runs on: the one a launch named, or the signed-in machine with its computer's hostname. */
  machine?: string | null;
  /** What the signed-in user may do with it; set when the proxy has accounts. */
  you?: SessionYou;
}

/** Who can see a session: only its owner, one of the owner's teams, or one of the owner's organizations. */
export type Visibility = "private" | "team" | "org";

export interface Sharing {
  owner: string | null;
  visibility: Visibility;
  /** With visibility team: the team, as <org>/<team>. */
  team: string | null;
  /** With visibility org (or team): the organization; null on sessions shared with everyone before organizations. */
  org?: string | null;
  /** view: others watch; control: they can also type into its terminal and end it. */
  access: "view" | "control";
}

export interface SessionYou {
  owner: boolean;
  control: boolean;
  sharing: Sharing;
}

export interface Identity {
  user: string;
  via: "cookie" | "token";
  role: "admin" | "member";
  orgs: string[];
  /** Teams as <org>/<team>. */
  teams: string[];
}

export interface AuthStatus {
  /** The proxy has accounts, so everyone signs in. */
  required: boolean;
  identity: Identity | null;
  secure: boolean;
}

export interface Team {
  id: string;
  /** <org>/<team>, as users' teams and sessions name it. */
  key: string;
  name: string;
  members: string[];
}

export interface Org {
  id: string;
  name: string;
  created_at: number | null;
  members: string[];
  teams: Team[];
}

export interface User {
  name: string;
  role: "admin" | "member";
  orgs: string[];
  teams: string[];
  created_at: number | null;
}

/** What changes to organizations and users answer with: everything the signed-in user can see. */
export interface Directory {
  orgs: Org[];
  users: User[];
}

/**
 * One piece of a chat turn. Text cut at 200k characters says how long it was in truncated; preview
 * means a page carried only the start of it (or of a tool call's input), and the whole turn has the rest.
 */
export type ChatBlock = { preview?: boolean } & (
  | { type: "text"; text: string; reminder?: boolean; truncated?: number }
  | { type: "thinking"; text: string; truncated?: number }
  | { type: "tool_use"; id: string; name: string; input: unknown }
  | { type: "tool_result"; tool_use_id: string; text: string; is_error: boolean; truncated?: number }
  | { type: "image" }
  | { type: "error"; text: string }
  | { type: "other"; text: string; truncated?: number }
);

export interface ChatTurn {
  /** Its position in the conversation, counted from the start. */
  index: number;
  role: "system" | "user" | "assistant" | "tool";
  blocks: ChatBlock[];
  /** The recorded call that wrote this reply, when it was recorded. */
  request_id?: string;
  /** System prompts before the first message. */
  preamble?: boolean;
}

/** A conversation within a session: the main chat, a sub-agent or a one-off call such as a title. */
export interface ChatThread {
  id: string;
  title: string;
  model: string;
  requests: string[];
  messages: number;
  started: string;
  updated: string;
  main: boolean;
}

/** A page of a chat: the turns from start, the newest ones unless the request asked for those before a position. */
export interface SessionChat {
  threads: ChatThread[];
  thread: string | null;
  total: number;
  start: number;
  turns: ChatTurn[];
  model_calls: number;
  traced_calls: number;
}

export interface LauncherToken {
  id: string;
  label: string;
  created_at: number;
  last_used_at: number | null;
}

export interface Trace {
  metadata: Record<string, unknown>;
  request: unknown;
  response: unknown;
  upstream_request?: unknown;
  http?: HttpExchange;
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
  /** The install command for each environment the server knows one for (install is the general one). */
  installs?: Partial<Record<InstallEnvironment, string>>;
  /** Signed in to its own service; all its HTTPS goes through the intercept proxy. */
  intercept?: boolean;
  needs_model?: boolean;
  /** Backend type the agent works with; null for account agents. */
  backend_type?: string | null;
  route?: "ollama" | "hosted" | "account";
  /** Configured backends this agent's protocol can use; empty for account agents. */
  backends?: string[];
  /** The backend a launch picks when none is named ("direct" for account agents); null when none serves it. */
  default_backend?: string | null;
}

/** The model servers installed on a machine that the server can start and stop (agent-machine servers on). */
export interface MachineServers {
  enabled?: boolean;
  ollama?: {
    found: boolean;
    running: boolean;
    /** Started by agent-machine, so it can stop it too; an Ollama started otherwise is left alone. */
    managed: boolean;
    version: string | null;
    bind: string | null;
    /** "systemd" when Ollama there is ollama.service; agent-machine restarts it once allowed (service_allowed). */
    service?: "systemd" | null;
    service_allowed?: boolean;
  };
  strata?: { found: boolean; profiles: { id: string; state: string; url: string | null; error: string | null }[]; error: string | null };
}

/** A machine signed in to this server: it runs agents, LLM providers, or both. */
export interface FleetMachine {
  id: string;
  name: string;
  owner: string | null;
  os: string;
  arch: string;
  version: string;
  address: string;
  /** The computer's own name, from its hello. */
  hostname?: string;
  /** The server's agents installed on it. */
  agents: string[];
  /** Model servers running on it; backend names the backend that already uses one. */
  providers: { type: string; port: number; version: string; models: number | null; url: string; backend: string | null }[];
  /** The backends it supplies: model servers added from it and containers it runs. */
  backends: string[];
  /** Whether it runs containers for the server; empty until the machine first says. */
  containers: Partial<MachineContainers>;
  /** Whether the server may start and stop the Ollama and Strata installed there, and their state. */
  servers: MachineServers;
  /** Whether the agent command launches run is installed there (null: the machine didn't say). */
  launcher?: boolean | null;
  /** Whether it lets you start agents on it, and in which folders. */
  launches: boolean;
  roots: string[];
  /** Whether its owner may open a terminal on it from here (agent-machine commands on). */
  commands?: boolean;
  connected: boolean;
  first_seen: number;
  last_seen: number;
  history: FleetLaunch[];
  /** What you may do there: each feature (MachineFeature), and manage (set who can use it, remove it). */
  you: { owner: boolean; manage?: boolean } & Partial<Record<MachineFeature, boolean>> & { launch: boolean };
  /** Who else may use each feature: only for those who can change it. */
  access?: MachineAccess;
  /** Whether it lets the server update its programs to the server's build (agent-machine updates on). */
  updates?: boolean;
  /** Whether it carries agents' requests to its model servers itself, so they need no port forward (tunnels.py). */
  tunnel?: boolean;
  /** The API protocol level its agent-machine speaks (older machines don't say). */
  protocol?: number | null;
  /** Whether its agent-machine is another version than the server's, and the command that updates it there. */
  update?: { available: boolean; server_version: string; command: string };
}

/** The server's /health: public, so a phone or a fresh machine can check it before signing in. */
export interface Health {
  status: string;
  proxy_instance_id: string;
  version?: string;
  protocol?: number;
  min_protocol?: number;
}

export type DownloadKind = "archive" | "deb" | "rpm" | "apk" | "arch" | "msi" | "pkg" | "binary" | "installer" | "android" | "wheel";

/** One file the server offers, from its build folder's SHA256SUMS. */
export interface DownloadFile {
  /** <os>-<arch>/<file>, relative to the build folder. */
  path: string;
  /** linux-amd64, windows-arm64, ... android, or python (the one wheel for every platform). */
  platform: string;
  os: string;
  arch: string;
  component: "agent" | "agent-machine" | "android" | "python";
  kind: DownloadKind;
  name: string;
  size: number;
  sha256: string;
  /** /downloads/<version>/<path>, on this server. */
  url: string;
}

/** /downloads/index.json: the build of the server's own version it serves (or one of the same X.Y.Z), or none. */
export interface DownloadIndex {
  server_version: string;
  protocol: number;
  /** The build folder served, or null when there's none. */
  version: string | null;
  /** False when the folder is another build of the same X.Y.Z. */
  exact: boolean;
  files: DownloadFile[];
  /** How to build them, when there are none. */
  hint?: string | null;
}

/** What a machine's owner can let organizations and teams do there (fleet.FEATURES). */
export type MachineFeature = "view" | "launch" | "terminal" | "models" | "servers" | "update";

/** Who may use each feature besides its owner: organizations and teams (<org>/<team>). */
export type MachineAccess = Partial<Record<MachineFeature, { orgs: string[]; teams: string[] }>>;

/** A machine taken out of the fleet: refused when it connects, until allowed back. */
export interface RemovedMachine {
  id: string;
  name: string;
  owner: string | null;
  at: number;
  by: string | null;
}

/** /api/machines/{id}/folders: the folders inside one of the machine's launch folders (its roots without a path). */
export interface MachineFolders {
  /** The folder listed; null for the list of roots. */
  path: string | null;
  /** The folder above it, when that's still inside a root. */
  parent: string | null;
  folders: { name: string; path: string }[];
  /** It has more folders than one listing shows. */
  truncated?: boolean;
}

export interface FleetLaunch {
  launch: string;
  at: number;
  by: string | null;
  agent: string;
  project: string;
  session_id: string | null;
  error: string | null;
}

export interface LaunchRequest {
  agent: string;
  project: string;
  model?: string;
  backend?: string;
  trace?: boolean;
  visibility?: string;
  team?: string;
  access?: "view" | "control";
}

export const PROTOCOL_LABELS: Record<Agent["protocol"], string> = {
  "openai-chat": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  anthropic: "Anthropic Messages",
  ollama: "Native Ollama",
};

export interface PreparedSession {
  session: Session;
  env: Record<string, string>;
  command: string[];
  /** The backend is a container the server runs; the launcher starts it first. */
  container: boolean;
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
  type?: string;
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

export interface OllamaSetting {
  key: string;
  group: string;
  label: string;
  kind: "bool" | "int" | "str" | "choice";
  default: string | number | boolean;
  help: string;
  env: string[];
  choices: string[];
  editable: boolean;
  minimum: number;
  maximum: number;
}

export type OllamaConfig = Record<string, string | number | boolean>;

export type OllamaServerState = NonNullable<MachineServers["ollama"]>;

/** The settings agent-machine starts a machine's Ollama with, kept in its own folder there. */
export interface OllamaSettings {
  schema: { groups: string[]; settings: OllamaSetting[] };
  config: OllamaConfig;
  env: Record<string, string | null>;
  warnings: string[];
  /** true/false when agent-machine started the running Ollama; null when stopped or started elsewhere. */
  applied_matches_saved: boolean | null;
  config_path: string;
  log_path: string;
  /** The machine's servers.ollama state. */
  status?: OllamaServerState;
}

/** A model server on a machine: the machine's id and the provider's, such as ollama-11434. */
export interface ProviderRef {
  machine: string;
  provider: string;
}

/** What every provider status answer has, besides the provider's own state. */
export interface ProviderBase {
  machine: string;
  provider: string;
  kind: string;
  port: number;
  url: string;
  /** The backend that uses it, if any. */
  backend: string | null;
  sampled_at: number;
  traffic: { in_flight: number; recent_requests: number; average_tps: number | null } | null;
}

export interface OllamaProviderModel {
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
  gpu_percent: number | null;
  expires_at: string | null;
}

export interface OllamaState extends ProviderBase {
  online: boolean;
  error?: string;
  version?: string;
  models: OllamaProviderModel[];
}

/** The backend's machine and provider, or null when it isn't on a machine (hosted, or set up before machines). */
export function providerRef(name: string, backends: Record<string, Backend> | undefined): ProviderRef | null {
  const backend = backends?.[name];
  if (!backend?.machine || !backend.provider) return null;
  return { machine: backend.machine, provider: backend.provider };
}

/** One model server on a machine, for the machine's page: found running there, added as a backend, or both. */
export interface MachineProvider {
  /** "{kind}-{port}", such as ollama-11434: its address on the machine. */
  id: string;
  kind: string;
  port: number;
  url: string | null;
  /** The backend agents use it as, or null until it's added. */
  backend: string | null;
  /** The machine runs it in a container for the server. */
  container: boolean;
  /** The machine reports it running now. */
  detected: boolean;
  version: string | null;
}

/** The model servers on a machine: what it reports running, and the backends that are its (containers among them), by kind and port. */
export function machineProviders(machine: FleetMachine, backends: Record<string, Backend> | undefined): MachineProvider[] {
  const found = new Map<string, MachineProvider>();
  for (const item of machine.providers) {
    const id = `${item.type}-${item.port}`;
    found.set(id, { id, kind: item.type, port: item.port, url: item.url, backend: item.backend, container: false, detected: true, version: item.version || null });
  }
  for (const [name, backend] of Object.entries(backends ?? {})) {
    if (backend.machine !== machine.id || !backend.provider) continue;
    const current = found.get(backend.provider);
    // Two backends on one port (a container in a model server's place): the first one listed names it.
    if (current?.backend && current.backend !== name) continue;
    const [kind, port] = [backend.provider.slice(0, backend.provider.lastIndexOf("-")), Number(backend.provider.slice(backend.provider.lastIndexOf("-") + 1))];
    found.set(backend.provider, {
      id: backend.provider, kind, port, url: current?.url ?? backend.url, backend: current?.backend ?? name,
      container: Boolean(backend.container), detected: current?.detected ?? false, version: current?.version ?? null,
    });
  }
  return [...found.values()].sort((a, b) => a.kind.localeCompare(b.kind) || a.port - b.port);
}

const machineUrl = (machine: string) => `/api/machines/${encodeURIComponent(machine)}`;
const providerUrl = (ref: ProviderRef) => `${machineUrl(ref.machine)}/providers/${encodeURIComponent(ref.provider)}`;

/** The catalogues a machine searches for models to pull. */
export type Catalog = "ollama" | "huggingface" | "huggingface-quants" | "docker-hub" | "docker-hub-tags";

export interface PullProgress {
  status?: string;
  digest?: string;
  total?: number;
  completed?: number;
  error?: string;
}

export interface LibraryModel {
  name: string;
  description: string;
  capabilities: string[];
  sizes: string[];
  pulls: number | null;
}

export interface LibrarySearch {
  query: string;
  models: LibraryModel[];
  note: string | null;
}

export interface HfRepo {
  repo: string;
  downloads: number | null;
  likes: number | null;
}

export interface HfSearch {
  query: string;
  repos: HfRepo[];
  note: string | null;
}

export interface HfQuant {
  quant: string;
  size: number;
  split: boolean;
}

export interface HfQuants {
  repo: string;
  quants: HfQuant[];
  note: string | null;
}

export interface StrataInstance {
  profile: string;
  url: string;
  running: boolean;
  model: string | null;
  model_state: string | null;
  pid: number | null;
  config_path: string | null;
  has_key: boolean;
  logs: StrataLog[];
}

export type StrataLog = "server" | "engine";

/** The parts of Strata's /v1/status the UI reads; Strata sends more. */
export interface StrataStatus {
  model?: string;
  loaded?: boolean;
  engine?: string;
  uptime_s?: number;
  context?: { native?: number; max_positions?: number };
  concurrency?: { serving?: number; requested?: number };
  dialects?: string[];
  vision?: { enabled?: boolean; available?: boolean; error?: string | null };
  activity?: { requests?: number; in_flight?: number; last_request_at?: number | null };
  last_timings?: {
    prompt_n?: number;
    prompt_per_second?: number;
    predicted_n?: number;
    predicted_per_second?: number;
    cache_n?: number;
    draft_n?: number;
    draft_n_accepted?: number;
    at?: number;
  } | null;
  machine?: {
    gpu?: { name?: string; used_mib?: number; total_mib?: number; util_pct?: number; temp_c?: number; power_w?: number } | null;
    ram?: { used_gib?: number; total_gib?: number } | null;
  };
}

export type StrataKeyState = "ok" | "missing" | "rejected" | "not_required" | "unknown";

export interface StrataModel {
  id: string;
  status?: { value?: string };
  meta?: { n_ctx?: number };
  architecture?: { input_modalities?: string[] };
  aliases?: string[];
  alias_of?: string;
}

export interface StrataState extends ProviderBase {
  online: boolean;
  error?: string;
  health?: { model?: string; loaded?: boolean; max_context?: number; images?: boolean; api_key?: boolean };
  status?: StrataStatus | null;
  key: { state: StrataKeyState; source: string | null };
  models: StrataModel[];
  instance: StrataInstance | null;
}

export interface DockerModel {
  id: string;
  /** The name agents send, e.g. ai/qwen3 or ai/qwen3:8B-Q4_K_M. */
  name: string;
  tags: string[];
  created: number | null;
  format: string | null;
  quantization: string | null;
  parameters: string | null;
  architecture: string | null;
  /** As Model Runner reports it, e.g. "4.68 GiB". */
  size: string | null;
  context_size: number | null;
  loaded: boolean;
  loading: boolean;
  in_use: boolean;
  engine: string | null;
}

export interface DockerState extends ProviderBase {
  online: boolean;
  error?: string;
  /** Inference engine name to Model Runner's description of its state. */
  engines: Record<string, string>;
  models: DockerModel[];
  disk: { models_disk_usage?: number; default_backend_disk_usage?: number } | null;
}

export interface DockerHubModel {
  name: string;
  description: string;
  pulls: number | null;
  stars: number | null;
  updated: string | null;
}

export interface DockerHubSearch {
  query: string;
  models: DockerHubModel[];
  note: string | null;
}

export interface DockerHubTag {
  tag: string;
  size: number | null;
  /** The engine this tag needs if not llama.cpp (GGUF): vllm for safetensors weights, mlx for Apple MLX ones. */
  engine: "vllm" | "mlx" | null;
  updated: string | null;
}

/**
 * An address another device could reach the proxy on. configured is proxy.public_url in backends.yaml;
 * tailscale works away from home; virtual is a VM or WSL adapter.
 */
export interface RemoteAddress {
  address: string;
  interface: string | null;
  kind: "configured" | "lan" | "tailscale" | "public" | "virtual";
  /** The console's address there, without a trailing slash. */
  url: string;
}

/** Whether this browser may watch and type into agents' terminals. The key and addresses only reach this computer. */
export interface RemoteStatus {
  trusted: boolean;
  local: boolean;
  paired: boolean;
  key?: string;
  listen_host?: string;
  port?: number;
  addresses?: RemoteAddress[];
}

/** A terminal the launcher shares with the proxy. */
export interface TerminalInfo {
  session_id: string;
  cols: number;
  rows: number;
  command: string | null;
  started_at: string | null;
  last_output_at: string | null;
  ended_at: string | null;
  exit_status: number | null;
  /** The launcher is connected, so input reaches the agent. */
  connected: boolean;
  viewers: number;
  bytes: number;
}

export interface TerminalSummary extends TerminalInfo {
  agent: string;
  agent_name: string;
  project: string | null;
  project_path: string | null;
  model: string | null;
  backend: string;
  session_ended_at: string | null;
  request_count: number;
  last_activity_at: string | null;
  /** Model requests streaming right now. */
  in_flight: number;
  /** The fleet machine it runs on, if any; machine_name is its computer's name either way. */
  machine: string | null;
  machine_name: string | null;
  /** Set while the agent waits for its user. */
  attention: TerminalAttention | null;
  you?: SessionYou;
}

/**
 * The server's guess that an agent is waiting for its user: quiet, with no model request in flight.
 * question: the end of its screen asks something (a permission prompt); idle: it's at its prompt.
 */
export interface TerminalAttention {
  kind: "question" | "idle";
  since: string | null;
  /** The line on its screen that asks, for a question. */
  question: string | null;
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
  health: () => request<Health>("/health"),
  // Served outside /api (public, for machines that aren't signed in yet); the console's page is /get.
  downloads: () => request<DownloadIndex>("/downloads/index.json"),
  sessions: () => request<Session[]>("/api/sessions"),
  session: (sessionId: string) => request<Session>(sessionUrl(sessionId)),
  requests: () => request<RequestRecord[]>("/api/requests"),
  chat: (sessionId: string, page: { thread?: string | null; before?: number; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (page.thread) query.set("thread", page.thread);
    if (page.before != null) query.set("before", String(page.before));
    if (page.limit != null) query.set("limit", String(page.limit));
    return request<SessionChat>(`${sessionUrl(sessionId)}/chat${query.size ? `?${query}` : ""}`);
  },
  chatTurn: (sessionId: string, thread: string, index: number) =>
    request<ChatTurn>(`${sessionUrl(sessionId)}/chat/turns/${index}?thread=${encodeURIComponent(thread)}`),
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
  deleteAllSessions: () => request<{ deleted: number; cleared: number; skipped: string[] }>("/api/sessions", { method: "DELETE" }),
  telemetryUrl: (sessionId: string) => `${sessionUrl(sessionId)}/telemetry`,
  agents: () => request<Agent[]>("/api/agents"),
  intercept: () => request<InterceptInfo>("/api/intercept"),
  prepareAgent: (agentId: string, body: { model: string; backend: string; trace: boolean; project?: string }) =>
    request<PreparedSession>(`/api/agents/${encodeURIComponent(agentId)}/sessions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  models: () => request<ModelsResponse>("/api/models"),
  config: () => request<ConfigResponse>("/api/config"),
  saveConfig: (config: NetworkConfig) =>
    request<ConfigResponse>("/api/config", { method: "PUT", body: JSON.stringify(config) }),
  // LLM providers: everything goes through the machine that runs the model server.
  machineMetrics: (machine: string) => request<MachineSample>(`${machineUrl(machine)}/metrics`),
  ollamaSettings: (machine: string) => request<OllamaSettings>(`${machineUrl(machine)}/servers/ollama`),
  saveOllamaSettings: (machine: string, config: OllamaConfig) =>
    request<OllamaSettings>(`${machineUrl(machine)}/servers/ollama`, { method: "PUT", body: JSON.stringify(config) }),
  providerStatus: <T extends ProviderBase>(ref: ProviderRef) => request<T>(providerUrl(ref)),
  /** Delete, load or unload a model; Strata's load and unload take no name. */
  providerModelAction: (ref: ProviderRef, action: "delete" | "load" | "unload", name?: string) =>
    request<Record<string, unknown>>(`${providerUrl(ref)}/models/${action}`, {
      method: "POST",
      body: JSON.stringify(name ? { name } : {}),
    }),
  searchLibrary: (machine: string, query: string) => searchCatalog<LibrarySearch>(machine, "ollama", { q: query }),
  searchHuggingFace: (machine: string, query: string) => searchCatalog<HfSearch>(machine, "huggingface", { q: query }),
  hfQuants: (machine: string, repo: string) => searchCatalog<HfQuants>(machine, "huggingface-quants", { repo }),
  searchDockerHub: (machine: string, query: string) => searchCatalog<DockerHubSearch>(machine, "docker-hub", { q: query }),
  dockerHubTags: (machine: string, repo: string) =>
    searchCatalog<{ repo: string; tags: DockerHubTag[] }>(machine, "docker-hub-tags", { repo }),
  remote: () => request<RemoteStatus>("/api/remote"),
  pair: (key: string) => request<{ paired: boolean }>("/api/remote/pair", { method: "POST", body: JSON.stringify({ key }) }),
  pairingCode: () => request<{ code: string; expires_in: number }>("/api/remote/code", { method: "POST", body: "{}" }),
  unpair: () => request<{ paired: boolean }>("/api/remote/unpair", { method: "POST", body: "{}" }),
  replaceRemoteKey: () => request<{ key: string }>("/api/remote/rotate", { method: "POST", body: "{}" }),
  terminals: () => request<{ terminals: TerminalSummary[] }>("/api/terminals"),
  machines: () => request<{ machines: FleetMachine[]; removed?: RemovedMachine[]; features?: Record<MachineFeature, string> }>("/api/machines"),
  setMachineAccess: (machineId: string, access: MachineAccess) =>
    request<FleetMachine>(`/api/machines/${encodeURIComponent(machineId)}/access`, { method: "PUT", body: JSON.stringify({ access }) }),
  restoreMachine: (machineId: string) => request<{ restored: string }>(`/api/machines/${encodeURIComponent(machineId)}/restore`, { method: "POST", body: "{}" }),
  launchOnMachine: (machineId: string, body: LaunchRequest) =>
    request<FleetLaunch>(`/api/machines/${encodeURIComponent(machineId)}/launch`, { method: "POST", body: JSON.stringify(body) }),
  machineFolders: (machineId: string, path?: string) =>
    request<MachineFolders>(`/api/machines/${encodeURIComponent(machineId)}/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  updateMachine: (machineId: string, agent: boolean) =>
    request<{ updating: boolean; from: string | null; to: string | null; message: string | null }>(`/api/machines/${encodeURIComponent(machineId)}/update`, {
      method: "POST",
      body: JSON.stringify({ agent }),
    }),
  openShell: (machineId: string, project: string) =>
    request<FleetLaunch>(`/api/machines/${encodeURIComponent(machineId)}/shell`, { method: "POST", body: JSON.stringify({ project }) }),
  adoptProvider: (machineId: string, type: string) =>
    request<{ backend: string; url: string }>(`/api/machines/${encodeURIComponent(machineId)}/providers/adopt`, { method: "POST", body: JSON.stringify({ type }) }),
  machineServer: (machineId: string, kind: "ollama" | "strata", action: "start" | "stop" | "restart", profile?: string) =>
    request<{ servers: MachineServers }>(`/api/machines/${encodeURIComponent(machineId)}/servers/${kind}/${action}`, {
      method: "POST",
      body: JSON.stringify(profile ? { profile } : {}),
    }),
  removeMachine: (machineId: string) => request<{ removed: string }>(`/api/machines/${encodeURIComponent(machineId)}`, { method: "DELETE" }),
  containers: (fresh = false) => request<ContainersOverview>(`/api/containers${fresh ? "?fresh=1" : ""}`),
  createContainer: (body: { name: string; machine: string } & Partial<ContainerSpec>) =>
    request<{ name: string; url: string; machine: string; spec: ContainerSpec }>("/api/containers", { method: "POST", body: JSON.stringify(body) }),
  updateContainer: (name: string, body: Partial<ContainerSpec>) =>
    request<{ name: string; spec: ContainerSpec }>(`/api/containers/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteContainer: (name: string) => request<{ deleted: string }>(`/api/containers/${encodeURIComponent(name)}`, { method: "DELETE" }),
  containerAction: (name: string, action: "start" | "stop" | "restart", body: { switch?: boolean } = {}) =>
    request<{ job: ContainerJob }>(`/api/containers/${encodeURIComponent(name)}/${action}`, { method: "POST", body: JSON.stringify(body) }),
  restartServer: () =>
    request<{ restarting: boolean; waiting_for: { requests: number }; listen: { host: string; port: number } }>(
      "/api/server/restart",
      { method: "POST", body: "{}" },
    ),
  authStatus: () => request<AuthStatus>("/api/auth/status"),
  login: (username: string, password: string) =>
    request<{ identity: Identity }>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => request<{ identity: null }>("/api/auth/logout", { method: "POST", body: "{}" }),
  changePassword: (current: string, next: string) =>
    request<{ changed: boolean }>("/api/auth/password", { method: "POST", body: JSON.stringify({ current, new: next }) }),
  tokens: () => request<{ tokens: LauncherToken[] }>("/api/auth/tokens"),
  createToken: (label: string) => request<{ id: string; token: string }>("/api/auth/tokens", { method: "POST", body: JSON.stringify({ label }) }),
  revokeToken: (id: string) => request<{ revoked: string }>(`/api/auth/tokens/${encodeURIComponent(id)}`, { method: "DELETE" }),
  orgs: () => request<{ orgs: Org[] }>("/api/orgs"),
  addOrg: (id: string, name: string) => request<Directory>("/api/orgs", { method: "POST", body: JSON.stringify({ id, name }) }),
  renameOrg: (id: string, name: string) => request<Directory>(`/api/orgs/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ name }) }),
  removeOrg: (id: string) => request<Directory>(`/api/orgs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  addTeam: (org: string, id: string, name: string) =>
    request<Directory>(`/api/orgs/${encodeURIComponent(org)}/teams`, { method: "POST", body: JSON.stringify({ id, name }) }),
  removeTeam: (org: string, id: string) => request<Directory>(`/api/orgs/${encodeURIComponent(org)}/teams/${encodeURIComponent(id)}`, { method: "DELETE" }),
  users: () => request<{ users: User[] }>("/api/users"),
  addUser: (body: { name: string; password: string; role: string; orgs: string[]; teams: string[] }) =>
    request<Directory>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (name: string, body: { role?: string; orgs?: string[]; teams?: string[]; password?: string }) =>
    request<Directory>(`/api/users/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) }),
  removeUser: (name: string) => request<Directory>(`/api/users/${encodeURIComponent(name)}`, { method: "DELETE" }),
  shareSession: (sessionId: string, sharing: { visibility: Visibility; team?: string | null; org?: string | null; access: Sharing["access"] }) =>
    request<Session>(sessionUrl(sessionId), { method: "PATCH", body: JSON.stringify(sharing) }),
  containerLog: async (name: string) => {
    const response = await fetch(`/api/containers/${encodeURIComponent(name)}/log?tail=1000`);
    if (!response.ok) throw new ApiError(`Couldn't read the log (${response.status})`, response.status);
    return response.text();
  },
};

/** The viewer WebSocket for a shared terminal, on the same host as the page. */
export const terminalSocketUrl = (sessionId: string) =>
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/terminals/${encodeURIComponent(sessionId)}/view`;

function searchCatalog<T>(machine: string, catalog: Catalog, params: Record<string, string>): Promise<T> {
  return request<T>(`${machineUrl(machine)}/catalog/${catalog}?${new URLSearchParams(params)}`);
}

/** Providers whose models can be pulled: Ollama and Docker Model Runner. */
export type PullProvider = "ollama" | "docker";

/**
 * The models to offer for a provider: the ones it has, then the aliases from models.yaml
 * whose Ollama tag it has (an alias for a model it hasn't pulled would fail). Aliases only
 * name Ollama tags, so other providers get just their own models.
 */
export function providerModels(listing: BackendModels | undefined, aliases: Record<string, Profile> | undefined, type: string | undefined): string[] {
  const names = (listing?.models ?? []).map((item) => item.name).filter(Boolean);
  if ((type ?? "ollama") !== "ollama") return names;
  const have = new Set(names);
  const usable = Object.entries(aliases ?? {})
    .filter(([alias, profile]) => !have.has(alias) && have.has(profile.ollama_model))
    .map(([alias]) => alias);
  return [...names, ...usable];
}

export type InstallEnvironment = "linux" | "macos" | "windows" | "wsl";

/** The model to offer first: the newest Qwen 3.x, coding builds ahead, else the first one. */
export function preferredModel(names: string[]): string {
  const version = (name: string) => Number(/^qwen3\.(\d+)/.exec(name)?.[1] ?? -1);
  const coder = (name: string) => (/cod(er|ing)/.test(name) ? 1 : 0);
  const ranked = names
    .filter((name) => version(name) >= 0)
    .sort((a, b) => version(b) - version(a) || coder(b) - coder(a) || a.localeCompare(b));
  return ranked[0] ?? names.find((name) => name.startsWith("qwen")) ?? names[0];
}

/** A backend name not taken yet: the type itself, then type-2, type-3, … */
export function freeBackendName(type: string, taken: string[]): string {
  let name = type;
  for (let index = 2; taken.includes(name); index++) name = `${type}-${index}`;
  return name;
}

/**
 * Add a hosted API (such as Anthropic's) to config/backends.yaml, keeping the others, and
 * return its name. Local model servers aren't added this way: they come from machines
 * (api.adoptProvider, api.createContainer), and the server refuses them here.
 */
export async function addBackend(type: string, url: string, name?: string): Promise<string> {
  const { config } = await api.config();
  const chosen = name?.trim() || freeBackendName(type, Object.keys(config.backends));
  if (chosen in config.backends) throw new ApiError(`There's already a backend called ${chosen}.`, 409);
  await api.saveConfig({ ...config, backends: { ...config.backends, [chosen]: { type, url: url.trim() } } });
  return chosen;
}

/** A provider's log as its machine reads it: Strata's server or engine log, or the Ollama agent-machine started. */
export const fetchProviderLog = async (ref: ProviderRef, source?: StrataLog) => {
  const response = await fetch(`${providerUrl(ref)}/log${source ? `?source=${source}` : ""}`);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(typeof body?.detail === "string" ? body.detail : `Request failed (${response.status})`, response.status);
  }
  return response.text();
};

/** POST a pull to the provider's machine and stream its NDJSON progress (Ollama-style for Docker too), calling onProgress for each line. */
export async function pullModel(
  ref: ProviderRef,
  name: string,
  onProgress: (progress: PullProgress) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${providerUrl(ref)}/models/pull`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ name }),
    signal,
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null);
    throw new ApiError(typeof body?.detail === "string" ? body.detail : `Pull failed (${response.status})`, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const consume = (line: string) => {
    const text = line.trim();
    if (!text) return;
    let obj: PullProgress;
    try {
      obj = JSON.parse(text);
    } catch {
      return;
    }
    onProgress(obj);
    if (obj.error) throw new ApiError(obj.error, 502);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index: number;
    while ((index = buffer.indexOf("\n")) >= 0) {
      consume(buffer.slice(0, index));
      buffer = buffer.slice(index + 1);
    }
  }
  consume(buffer);
}

export const requestKey = (record: { session_id: string; request_id: string }) =>
  `${record.session_id}:${record.request_id}`;

export const isFailure = (record: RequestRecord) => record.status >= 400 || Boolean(record.error_type);

/** Whether a request was a model call rather than other traffic an agent makes (auth, telemetry, ...). */
export const isModelCall = (record: { kind?: TrafficKind; model: string }) =>
  !record.kind || record.kind === "api" || Boolean(record.model);

/** Method and full address for intercepted traffic, otherwise the proxy endpoint. */
export const trafficTarget = (record: { kind?: TrafficKind; method?: string | null; host?: string | null; endpoint: string }) =>
  record.kind && record.kind !== "api" && record.host
    ? `${record.method ?? ""} ${record.kind === "tunnel" ? record.endpoint : record.host + record.endpoint}`.trim()
    : record.endpoint;

export interface GpuSample {
  index: number;
  name: string;
  utilization: number | null;
  memory_used: number | null;
  memory_total: number | null;
  temperature: number | null;
  power_watts: number | null;
}

/** CPU, memory and GPU use of a machine, sampled by its agent-machine. */
export interface MachineSample {
  hostname: string;
  platform: string;
  timestamp: number;
  cpu: { percent: number | null; count: number };
  memory: { used: number; total: number; percent: number };
  gpus: GpuSample[];
  gpu_source: string | null;
  gpu_error: string | null;
}

const OUTCOMES: Record<string, string> = {
  client_closed: "Client hung up",
  incomplete_stream: "Stream cut off",
  upstream_error: "Upstream error",
};

/** A short, plain label for why a request did not complete normally. */
export const outcomeLabel = (record: RequestRecord) =>
  record.error_type ? OUTCOMES[record.error_type] ?? `Failed: ${record.error_type}` : record.status >= 400 ? `Failed ${record.status}` : null;

/** Token count with a marker when it was estimated rather than reported by Ollama. */
export const tokens = (record: RequestRecord, value: number) =>
  `${record.tokens_estimated ? "≈ " : ""}${value.toLocaleString()}`;

export interface LiveDetails {
  key: string;
  session_id: string;
  request_id: string;
  enabled: boolean;
  done: boolean;
  request: unknown;
  upstream: unknown;
  http?: HttpExchange | null;
  raw: Record<string, unknown>[];
  raw_total: number;
  reasoning: string;
  content: string;
  tool: string;
  [field: string]: unknown;
}

export const fetchLiveDetails = (sessionId: string, requestId: string) =>
  fetch(`/api/live/${encodeURIComponent(sessionId)}/${encodeURIComponent(requestId)}`).then(async (response) => {
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        message = (await response.json()).detail ?? message;
      } catch {
        // Keep the status message.
      }
      throw new ApiError(message, response.status);
    }
    return response.json() as Promise<LiveDetails>;
  });
