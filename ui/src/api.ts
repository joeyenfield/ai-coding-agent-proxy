export interface ApiCredential {
  client_id: string;
  user: string;
  label: string;
  tenant: string;
  role: string;
  created_at: number;
  bootstrap: boolean;
  revoked: boolean;
  active: boolean;
}

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
export const MACHINE_TYPES = ["ollama", "llamacpp", "strata", "docker"];

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

/** A saved Strata profile on a machine. agent-machine saves one for each model installed in Strata. */
export interface StrataProfile {
  id: string;
  /** stopped, ready, stale (its runtime's host went away, as when the computer restarted; start recovers it), ...
   * or broken: its run config is gone, so it can't start. */
  state: string;
  url: string | null;
  error: string | null;
  /** The installed model it runs, when Strata still has it. */
  model?: string | null;
  /** Why it can't start: its model was removed or reinstalled in Strata. */
  broken?: string | null;
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
  /** manage: may create, change and remove containers there (its owner, and site admins). */
  you: { owner: boolean; manage?: boolean };
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

/** api: a proxy endpoint. intercept/upgrade/tunnel: captured by the HTTPS intercept proxy. mcp: a call to an MCP server a machine shares. */
export type TrafficKind = "api" | "intercept" | "upgrade" | "tunnel" | "mcp";

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
  /** Only when asked for (api.status(true)). */
  recent_requests?: RequestRecord[];
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
  /** admin: an admin of their own organization (their tenant). */
  role: "admin" | "member";
  /** The one organization the user belongs to, for good. Nothing crosses organizations. */
  tenant: string;
  /** An admin of the root organization: manages the server's settings and sets up organizations and their users. */
  site_admin: boolean;
  /** Always [tenant]. */
  orgs: string[];
  /** Teams as <org>/<team>. */
  teams: string[];
}

export interface AuthStatus {
  required: true;
  identity: Identity | null;
  secure: boolean;
  issuer: string;
  audience: string;
  client_id: string;
  /** Seconds left on the access token this check carried (null: signed out, or an older server). */
  expires_in?: number | null;
  /** That access token, for renewing open sockets in place. */
  access_token?: string | null;
}

export interface Team {
  id: string;
  /** <org>/<team>, as users' teams and sessions name it. */
  key: string;
  name: string;
  members: string[];
}

/** The organization above all others; it holds only site admins. */
export const ROOT_ORG = "root";

export interface Org {
  id: string;
  name: string;
  /** The root organization: site administration, admins only. */
  root: boolean;
  /** "root" for every other organization; null for root itself. */
  parent: string | null;
  created_at: number | null;
  members: string[];
  teams: Team[];
}

export interface User {
  name: string;
  role: "admin" | "member";
  /** The organization the user belongs to; they can't be moved. */
  tenant: string;
  /** Always [tenant]. */
  orgs: string[];
  teams: string[];
  created_at: number | null;
}

/** "Site admin", "Organization admin" or "Member", for an identity or a user. */
export function roleLabel(who: { role: "admin" | "member"; tenant: string; site_admin?: boolean }): string {
  if (who.site_admin || (who.role === "admin" && who.tenant === ROOT_ORG)) return "Site admin";
  return who.role === "admin" ? "Organization admin" : "Member";
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
  | {
      type: "text";
      text: string;
      reminder?: boolean;
      truncated?: number;
      /** Who wrote text sent as the user: the person, a sub-agent's parent, the agent itself, or unknown when nothing tells. */
      author?: ChatAuthor;
      /** What the agent's own text is: a reminder, a note, a command's output, an interruption, a summary, a compaction. */
      note?: "reminder" | "note" | "output" | "interrupted" | "summary" | "compacted";
      /** Typed while the agent was working (the agent passed it on with tool results). */
      queued?: boolean;
      /** A slash or ! command the person ran. */
      command?: boolean;
    }
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
  /** When it was sent: its call's time, or for a message the time of the call that first carried it. */
  at?: string;
  /** Handed to a sub-agent or background call from the conversation it came from. */
  inherited?: boolean;
  /** A marker the server put in: where the agent compacted the conversation. */
  event?: "compacted";
}

export type ChatAuthor = "person" | "parent" | "agent" | "unknown";

/** main: the session's chat; conversation: another one (before /clear, say); subagent; background: a call the agent made by itself. */
export type ChatKind = "main" | "conversation" | "subagent" | "background";

/** A conversation within a session: the main chat, another conversation, a sub-agent or a background call such as a title. */
export interface ChatThread {
  id: string;
  title: string;
  model: string;
  requests: string[];
  messages: number;
  started: string;
  updated: string;
  main: boolean;
  kind: ChatKind;
  /** What a background call was for, when its instructions say ("Naming the session"). */
  purpose: string;
  /** The sub-agent's id, from Claude Code. */
  agent: string | null;
}

/** A page of a chat: the turns from start, the newest ones unless the request asked for those before a position. */
export interface SessionChat {
  threads: ChatThread[];
  thread: string | null;
  kind: ChatKind | null;
  total: number;
  start: number;
  /** How many turns at the start were handed over from another conversation. */
  inherited: number;
  turns: ChatTurn[];
  model_calls: number;
  traced_calls: number;
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
  /** Whether a launch can give it MCP servers machines share. */
  mcp?: boolean;
  /** For account agents: the kind of provider account (subscription) it runs with. */
  subscription?: SubscriptionProvider | null;
  /** The organization whose definition it is; null for the server's own. */
  org?: string | null;
  /** Set when another server's organization shares it over a bridge (its id is then its full name). */
  bridge?: string | null;
}

/** The kinds of subscription (provider_accounts.PROVIDERS on the server). */
export type SubscriptionProvider = "claude" | "codex" | "copilot";
export type AccountState = "available" | "near_limit" | "unknown" | "rate_limited" | "quota_exhausted" | "auth_expired" | "offline";
export type UsageWindow = "5h" | "24h" | "7d" | "30d" | "month";

/** One provider as people use it (usage_limits.py): a backend (on a machine, or hosted) or a kind of subscription. */
export interface ProviderEntry {
  /** backend:<name> or subscription:<claude|codex|copilot>. */
  id: string;
  /** "removed": usage of a backend that's gone, in organization reports only. */
  kind: "local" | "hosted" | "subscription" | "removed";
  type: string;
  type_label: string;
  name: string;
  label: string;
  machine: { id: string; name: string; online: boolean } | null;
  container: boolean;
  default: boolean;
  /** Subscriptions: how many of yours (or shared with you) the server has. */
  accounts?: number;
}

export interface UsageCounts {
  requests: number;
  input_tokens: number;
  output_tokens: number;
}

export interface UsageLimit {
  /** A person's name, or "*" for everyone in the organization. */
  user: string;
  provider: string;
  window: UsageWindow;
  tokens: number;
  set_by: string | null;
  set_at: number | null;
}

/** A limit as it applies to one person, with what they've used of it. */
export interface LimitStatus extends UsageLimit {
  /** It's everyone's limit, not the person's own. */
  inherited: boolean;
  used: number;
  remaining: number;
  percent_used: number;
  reached: boolean;
  /** When it frees up (seconds since the epoch), once reached. */
  until: number | null;
}

export interface ProviderWithUsage extends ProviderEntry {
  usage: Record<UsageWindow, UsageCounts>;
  limits: LimitStatus[];
  blocked: LimitStatus | null;
}

export interface UsageWindowInfo {
  id: UsageWindow;
  label: string;
}

export interface ProvidersResponse {
  providers: ProviderWithUsage[];
  windows: UsageWindowInfo[];
  user: string;
  tenant: string;
}

export interface UsageRow {
  user: string;
  provider: string;
  usage: Record<UsageWindow, UsageCounts>;
  limits: LimitStatus[];
  blocked: LimitStatus | null;
}

export interface OrgUsage {
  org: string;
  /** Whether you see everyone's and may set limits (its admins, site admins); members see their own. */
  manage: boolean;
  users: string[];
  providers: ProviderEntry[];
  rows: UsageRow[];
  limits: UsageLimit[];
  windows: UsageWindowInfo[];
}

/** One provider CLI on a machine, as `agent accounts detect` found it there: never a token. */
export interface ProviderCli {
  provider: SubscriptionProvider;
  installed: boolean;
  version: string | null;
  auth: "signed_in" | "signed_out" | "unknown";
  method: string | null;
  /** false when it's signed in with an API key rather than a subscription. */
  subscription: boolean | null;
  modes: ("runner" | "token")[];
  reauthenticate: boolean;
}

/** Whether a provider account can run on one machine, from what the machine reported. */
export interface AccountHealth {
  machine_id: string;
  machine_name: string | null;
  connected: boolean;
  assigned: boolean;
  enabled: boolean;
  installed: boolean | null;
  cli_version: string | null;
  authenticated: boolean | null;
  available: boolean;
  last_checked_at: number | null;
  last_success_at: number | null;
  error_code: string | null;
  error_message: string | null;
}

export interface AccountAssignment {
  account_id: string;
  machine_id: string;
  assigned_by: string;
  assigned_at: number;
  enabled: boolean;
  provisioning_allowed: boolean;
  source: "console" | "enrollment";
  health: AccountHealth;
}

/** A limit the account's owner declared: the only base for a remaining balance. */
export interface DeclaredLimit {
  window: UsageWindow;
  unit: "requests" | "tokens";
  value: number;
  source?: "user_declared";
}

/** What went through the server for an account in one window (source observed: this server's traffic only). */
export interface ObservedUsage {
  window: UsageWindow;
  since: number;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  rate_limited: number;
  errors: number;
  source: "observed";
  confidence: string;
}

export interface UsageEstimate {
  limit: DeclaredLimit;
  used: number;
  remaining: number;
  percent_used: number | null;
  warning: boolean;
  stop: boolean;
  source: "estimate";
  confidence: string;
  basis: string;
}

export interface AccountUsage {
  account_id: string;
  observed: ObservedUsage[];
  /** The provider's own numbers, when a supported source reports them; none does yet. */
  reported: null | Record<string, unknown>;
  /** What's left of the declared limit, estimated from this server's traffic. */
  estimate: UsageEstimate | null;
  /** "unknown" without a declared limit. */
  remaining: "unknown" | null;
  rate_limits: {
    last_limited_at: number | null;
    retry_after: number | null;
    cooldown_until: number | null;
    exhausted_until: number | null;
    source: "observed";
    confidence: string;
  };
  /** The windows this provider's limits usually reset over. */
  windows: UsageWindow[];
}

/** A provider account: someone's own subscription. Metadata only: no answer ever carries a token. */
export interface ProviderAccount {
  id: string;
  provider: SubscriptionProvider;
  provider_label: string;
  owner: string;
  tenant: string;
  display_name: string;
  plan_name: string;
  authentication_mode: "runner" | "token";
  mode_label: string;
  status: "active" | "disabled";
  disabled_by: string | null;
  priority: number;
  /** Others who may launch with it (only shown to its owner). */
  users: string[];
  limit: DeclaredLimit | null;
  warning_percent: number;
  stop_percent: number;
  created_at: number;
  updated_at: number;
  last_validated_at: number | null;
  last_success_at: number | null;
  expires_at: number | null;
  credential: { stored: boolean; set_at?: number; key_version?: number; kind?: string };
  state: AccountState;
  state_reason: string;
  assignments: AccountAssignment[];
  you: { owner: boolean; use: boolean; manage: boolean; suspend: boolean };
  usage?: AccountUsage;
  existing?: boolean;
}

export interface SubscriptionProviderInfo {
  label: string;
  cli: string;
  modes: ("runner" | "token")[];
  plans: string[];
  windows: UsageWindow[];
  sign_in: string;
  note: string;
  credential_env?: string;
}

export interface ProviderAccountList {
  accounts: ProviderAccount[];
  providers: Record<SubscriptionProvider, SubscriptionProviderInfo>;
  modes: Record<"runner" | "token", string>;
  states: AccountState[];
  windows: UsageWindow[];
  vault: { configured: boolean };
}

export interface AccountEvent {
  at: number;
  kind: string;
  by: string | null;
  [detail: string]: unknown;
}

export interface PlanCandidate {
  kind: "subscription" | "fallback";
  agent: string;
  machine_id: string;
  machine_name: string;
  account_id: string | null;
  account_name?: string;
  provider?: SubscriptionProvider;
  state: AccountState;
  reason: string;
  remaining?: number | null;
}

export interface SchedulePlan {
  choice: PlanCandidate | null;
  candidates: PlanCandidate[];
  skipped: { agent?: string; machine_id?: string; account_id?: string; reason: string }[];
}

/** An MCP server a machine shares (agent-machine mcp add) that you may give the agents you start. */
export interface SharedMcp {
  /** "<machine id>/<name>", as a launch names it. */
  id: string;
  machine: string;
  machine_name: string;
  name: string;
  description: string;
  /** stdio: a command the machine runs for each session; http: a URL it proxies. */
  transport: "stdio" | "http";
  /** Whether its command is installed on the machine. */
  found: boolean;
  connected: boolean;
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
  strata?: {
    found: boolean;
    profiles: StrataProfile[];
    error: string | null;
    /** What this machine's agent-machine can do beyond start and stop (older ones don't say, and can't). */
    actions?: ("remove" | "prune" | "cleanup")[];
  };
  /** llama.cpp's llama-server, in router mode on loopback; known is false for a machine from before llama.cpp. */
  llamacpp?: LlamaServerState;
}

/** The llama.cpp on a machine: installed or found there, running, and an install the console started. */
export interface LlamaServerState {
  known: boolean;
  found: boolean;
  running: boolean;
  /** Started by agent-machine, so it can stop it too. */
  managed: boolean;
  port?: number;
  /** The build, such as b12032. */
  version?: string | null;
  /** github or termux when the console installed it (with which build), found when it was there already. */
  source?: "github" | "termux" | "found" | null;
  variant?: string | null;
  release?: string | null;
  /** termux, or the machine's os-arch. */
  platform?: string | null;
  install?: LlamaInstallJob | null;
}

/** An install of llama.cpp running (or just finished) on a machine. */
export interface LlamaInstallJob {
  state: "installing" | "installed" | "failed" | null;
  variant: string | null;
  release: string | null;
  step: string | null;
  done: number;
  total: number;
  error: string | null;
  at: string | null;
}

/** How llama.cpp can be installed on a machine: Termux's packages, or a GitHub release's builds for its platform. */
export interface LlamaInstallOptions {
  source: "github" | "termux";
  release?: string;
  published_at?: string;
  variants: { id: string; label: string; help?: string; size?: number; recommended: boolean }[];
  error?: string;
}

/**
 * A request for a machine's model server. Ollama: start, stop, restart. Strata: start or stop a
 * profile, remove one whose model is gone, prune (remove all of those), cleanup (clear the
 * leftovers of runtimes whose owner is gone, as after a restart).
 */
export type ServerActionName = "start" | "stop" | "restart" | "remove" | "prune" | "cleanup" | "install" | "uninstall";

/** A machine signed in to this server: it runs agents, LLM providers, or both. */
export interface FleetMachine {
  id: string;
  name: string;
  owner: string | null;
  /** The organization it belongs to; only people in it see it. */
  tenant?: string;
  os: string;
  arch: string;
  version: string;
  address: string;
  /** The computer's own name, from its hello. */
  hostname?: string;
  /** The server's agents installed on it. */
  agents: string[];
  /** Model servers running on it; backend names the backend that already uses one. */
  providers: { type: string; port: number; version: string; models: number | null; url: string; backend: string | null; flavor?: ApiFlavor }[];
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
  /** Whether the console may browse and read the files in its launch folders (agent-machine files on). */
  files?: boolean;
  /** The MCP servers it shares (agent-machine mcp add), by name; their commands stay on the machine. */
  mcp?: { port?: number; servers?: Omit<SharedMcp, "id" | "machine" | "machine_name" | "connected">[] };
  /** The provider CLIs there and whether each is signed in (null: its agent-machine is too old to say). */
  provider_clis?: { clis: ProviderCli[]; at: number | null; error: string | null } | null;
  connected: boolean;
  first_seen: number;
  last_seen: number;
  history: FleetLaunch[];
  /** What you may do there: each feature (MachineFeature), and manage (set who can use it, remove it). */
  /** containers: may create, change and remove containers there (its owner, and site admins). */
  you: { owner: boolean; manage?: boolean; containers?: boolean } & Partial<Record<MachineFeature, boolean>> & { launch: boolean };
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

export type DownloadKind = "archive" | "deb" | "rpm" | "apk" | "arch" | "msi" | "pkg" | "binary" | "installer" | "uninstaller" | "android" | "wheel";

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
export type MachineFeature = "view" | "launch" | "terminal" | "files" | "models" | "servers" | "update" | "mcp";

/** Who may use each feature besides its owner: organizations and teams (<org>/<team>). */
/** bridges: bridges (to an organization on a linked server) the owner lets use launch, terminal or mcp. */
export type MachineAccess = Partial<Record<MachineFeature, { orgs: string[]; teams: string[]; bridges?: string[] }>>;

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

/** One entry of /api/machines/{id}/files: a folder or a file (with its size), modified in seconds since the epoch. */
export interface MachineFileEntry {
  name: string;
  path: string;
  kind: "folder" | "file";
  size?: number;
  modified?: number;
}

/** /api/machines/{id}/files: the folders and files inside one of the machine's launch folders (its roots without a path). */
export interface MachineFiles {
  /** The folder listed; null for the list of roots. */
  path: string | null;
  /** The folder above it, when that's still inside a root. */
  parent: string | null;
  entries: MachineFileEntry[];
  /** It has more entries than one listing shows. */
  truncated?: boolean;
}

/** /api/machines/{id}/files/content: one file's text (at most its first MiB), or none for a binary file. */
export interface MachineFileContent {
  path: string;
  name: string;
  size: number;
  modified: number | null;
  binary: boolean;
  truncated: boolean;
  content: string | null;
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
  /** Shared MCP servers for the agent (SharedMcp ids). */
  mcp?: string[];
  /** For account agents: the provider account to run with, or "auto" for the best one now. */
  account_id?: string;
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
  /** The shared MCP servers it was given, and the URL the agent reaches each at (Streamable HTTP). */
  mcp?: { id: string; name: string; url: string }[];
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
  /** public_url: the console's address people use (null for none); left as it is when not sent. */
  proxy: { listen_host: string; listen_port: number; url: string; public_url?: string | null };
  default_backend: string;
  backends: Record<string, Backend>;
}

/** The generic backend type for OpenAI- and Anthropic-compatible APIs, hosted or on a machine. */
export const API_TYPE = "api";

export type ApiProtocol = "openai-chat" | "openai-responses" | "anthropic";
export type ApiAuthMode = "none" | "bearer" | "x-api-key" | "api-key" | "agent";
export type ApiCategory = "machine" | "hosted" | "gateway";

/** Whether the provider answered the server's last check. */
export interface ApiProviderHealth {
  ok: boolean;
  status: number | null;
  /** Seconds since the epoch. */
  at: number;
  latency_ms: number | null;
  error: string | null;
}

/** What an organization's admins edit of an API provider (PUT /api/orgs/{org}/providers/{name}). */
export interface ApiProviderFields {
  url: string;
  /** Where its Anthropic Messages API is, when that isn't at url. */
  anthropic_url: string | null;
  category: ApiCategory;
  protocols: ApiProtocol[];
  /** agent: the agent's own credentials are passed through. */
  auth: ApiAuthMode;
  /** An environment variable on the server holding the key; starts with AI_PROXY_API_KEY_. */
  key_env: string | null;
  /** The alias agents use, mapped to the provider's own model id. */
  models: Record<string, string>;
  /** Also list the provider's own GET /models. */
  discover: boolean;
  /** Extra headers, never secret ones. */
  headers: Record<string, string>;
  connect_timeout: number;
  verify_tls: boolean;
  /** Azure OpenAI's ?api-version=. */
  api_version: string | null;
  /** Higher is preferred when the server picks a backend by itself. */
  priority: number;
  /** Other API providers to try when this one can't be reached. */
  fallback: string[];
  enabled: boolean;
  /** The organization that owns a hosted one, null for the site's own (a machine's is its machine's). */
  org: string | null;
  /** Its organization's teams (<org>/<team>) whose people may use it; [] is everyone there. */
  teams: string[];
  /** Other organizations it's shared with whole, ["*"] every one; only site admins set it. */
  orgs: string[];
}

/** An API provider as the server reports it; the key itself is never sent back. */
export interface ApiProvider extends ApiProviderFields {
  name: string;
  type: typeof API_TYPE;
  /** The machine that runs it, or null for a hosted API or gateway. */
  machine: string | null;
  has_key: boolean;
  key_source: "vault" | "env" | null;
  /** Last model IDs fetched automatically from the provider. */
  discovered_models?: string[];
  balance_supported?: boolean;
  health: ApiProviderHealth | null;
  /** It's the server's default backend. */
  default: boolean;
  /** The organization it belongs to: its owner's, or its machine's (null once the machine is gone). */
  tenant: string | null;
}

/** A starting point for adding a well-known API. */
export interface ApiPreset {
  id: string;
  label: string;
  url: string;
  anthropic_url: string | null;
  protocols: ApiProtocol[];
  auth: ApiAuthMode;
  category: ApiCategory;
  api_version: string | null;
  headers: Record<string, string>;
  notes: string;
}

export interface ApiProvidersResponse {
  providers: ApiProvider[];
  /** Whether the server can store keys (AI_PROXY_VAULT_KEYS is set). */
  vault: boolean;
  presets: ApiPreset[];
  protocols: ApiProtocol[];
  auth_modes: ApiAuthMode[];
  /** Whether you may share providers with other organizations (site admins). */
  share: boolean;
}

/** The body of a save: the fields, and a new key (write-only) or clear_key to remove the stored one. */
export interface ApiProviderSave extends ApiProviderFields {
  key?: string;
  clear_key?: boolean;
}

/** POST /api/orgs/{org}/providers/{name}/test: whether it answered, and the models it lists. */
export interface ApiProviderTest {
  ok: boolean;
  status: number | null;
  latency_ms: number | null;
  models: string[] | null;
  error: string | null;
}

export interface ApiProviderBalance {
  is_available: boolean;
  balance_infos: { currency: "CNY" | "USD"; total_balance: string; granted_balance: string; topped_up_balance: string }[];
}

/** GET /api/requests's filters. */
export interface RequestQuery {
  session_id?: string;
  limit?: number;
  before?: string;
  model?: string;
  kind?: string;
}

function queryString(query: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query) as [string, unknown][]) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

/** GET /api/config/agents: an agent as stored. builtin ones ship with the server and follow its releases until edited. */
export interface StoredAgent {
  id: string;
  definition: Record<string, unknown>;
  /** The definition as YAML, to edit (GET only). */
  yaml?: string;
  builtin: boolean;
  edited: boolean;
}

/** GET /api/config/models: Ollama model tuning, layered defaults, then the first matching profile, then the alias. */
export interface ModelTuning {
  defaults: Record<string, unknown>;
  profiles: Record<string, unknown>[];
  aliases: Record<string, Record<string, unknown>>;
}

/** GET /api/config/storage: where the server keeps everything, and how long it keeps recordings. */
export interface StorageInfo {
  database: { kind: "sqlite" | "postgresql"; url: string; size: number | null };
  retention_days: number | null;
  request_log: RequestLogSettings;
}

/** The request log (console: API log): every request saved for days; payloads (debug mode) for payload_days. */
export interface RequestLogSettings {
  enabled: boolean;
  days: number;
  payloads: boolean;
  payload_days: number;
}

/** What a retention policy keeps, each for its own number of days. */
export const RETENTION_KINDS = ["traces", "terminal", "records", "sessions", "audit"] as const;
export type RetentionKind = (typeof RETENTION_KINDS)[number];
/** Days each kind is kept; null is forever (in max: no bound, in min: no floor). */
export type RetentionDays = Record<RetentionKind, number | null>;
/** off: never recorded; opt_in: only sessions started with --trace; always: every session. */
export type TraceCapture = "off" | "opt_in" | "always";
export const TRACE_CAPTURES: TraceCapture[] = ["off", "opt_in", "always"];

export interface CapturePolicy {
  traces: TraceCapture;
  /** Account agents' decrypted traffic in traces. */
  intercept_bodies: boolean;
  /** Shared terminals' output. */
  terminal_output: boolean;
}

/** GET/PUT /api/config/retention: the server's policy, and the bounds organizations set theirs within (site admins). */
export interface ServerRetention {
  capture: CapturePolicy;
  /** The trace modes organizations may choose. */
  allowed_traces: TraceCapture[];
  days: RetentionDays;
  max: RetentionDays;
  min: RetentionDays;
}

/** An organization's own overrides: only what it changes. */
export interface OrgRetentionOwn {
  capture?: Partial<CapturePolicy>;
  days?: Partial<RetentionDays>;
}

/** GET /api/orgs/{org}/retention: the policy its new sessions start under, its own part, and the server's bounds. */
export interface OrgRetention {
  capture: CapturePolicy;
  days: RetentionDays;
  own: OrgRetentionOwn;
  /** The server's bounds, and its own capture and days (what "the server's" is for a setting left alone). */
  server: Pick<ServerRetention, "allowed_traces" | "max" | "min" | "capture" | "days">;
  /** How many legal holds the organization has (GET only). */
  holds?: number;
}

export type HoldScope = "org" | "user" | "session";

/** A legal hold: nothing it covers is deleted by any policy until it's lifted. */
export interface LegalHold {
  id: string;
  tenant: string;
  scope: HoldScope;
  /** The organization's id, a username or a session id. */
  target: string;
  reason: string | null;
  set_by: string | null;
  /** Unix time. */
  set_at: number;
}

export type AuditOutcome = "success" | "refused" | "failure";

/** One audit event, a CloudEvent with its place in its organization's hash chain. */
export interface AuditEvent {
  id: string;
  specversion?: string;
  source?: string;
  /** mesh.audit.<category>.<action> */
  type: string;
  tenant: string;
  actor: string | null;
  outcome: AuditOutcome;
  /** ISO 8601. */
  time: string;
  time_epoch: number;
  data: {
    actor?: { user: string | null; via: string | null; ip: string | null; user_agent: string | null };
    /** Such as "PUT /api/config/retention". */
    action?: string;
    target?: { kind?: string; id?: string; part?: string };
    reason?: string | null;
    status?: number | null;
    changes?: { fields?: string[]; [key: string]: unknown };
    [key: string]: unknown;
  };
  chain: { seq: number; prev: string; hash: string };
}

export interface AuditQuery {
  /** Site admins only: one organization's events. */
  org?: string;
  /** A type prefix: "data", "configuration.update" or a full mesh.audit.… type. */
  type?: string;
  actor?: string;
  outcome?: AuditOutcome | "";
  /** Unix time. */
  since?: number;
  /** A time, or the cursor a page's next_before gave. */
  before?: number | string;
  limit?: number;
}

/** GET /api/audit: newest first; next_before pages back, null on the last page. */
export interface AuditPage {
  events: AuditEvent[];
  /** The cursor for the next page (pass it as before), null on the last page. */
  next_before: string | null;
}

/** GET /api/audit/verify: every chain recomputed and every checkpoint's signature checked. */
export interface AuditVerify {
  ok: boolean;
  tenants: Record<string, { events: number; first: number | null; head: number | null }>;
  checkpoints: number;
  problems: string[];
}

/** POST /api/audit/checkpoint: the chains' heads signed now; null when nothing was recorded since the last one. */
export interface AuditCheckpoint {
  id: string;
  time: number;
  heads: Record<string, [number, string]>;
  signature: string;
  key_id: string;
  location: string | null;
}

export type AuditExportKind = "otlp" | "syslog" | "webhook" | "blob";

/** Somewhere the audit log is sent as it grows. token_env names an environment variable on the server, never a secret. */
export interface AuditExport {
  name: string;
  kind: AuditExportKind;
  url: string;
  token_env: string | null;
  /** Only these organizations' events; null or empty: every one. */
  tenants: string[] | null;
  enabled: boolean;
  /** Where a blob export writes, in the server's blob storage. */
  prefix: string;
}

/** GET /api/config/audit (site admins). PUT takes and answers {exports, checkpoint_minutes}. */
export interface AuditConfig {
  exports: AuditExport[];
  checkpoint_minutes: number;
  key_id?: string;
  /** The checkpoints' Ed25519 public key, base64. */
  public_key?: string;
  /** Events the slowest export hasn't sent yet. */
  backlog?: number;
}

const auditQuery = (query: AuditQuery, extra: Record<string, string> = {}) => {
  const params = new URLSearchParams(extra);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
};

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

/** The LLAMA_ARG_* settings agent-machine starts a machine's llama-server with: the same shape as Ollama's. */
export interface LlamaSettings extends Omit<OllamaSettings, "status"> {
  /** The machine's servers.llamacpp state. */
  status?: LlamaServerState;
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

/** A model llama.cpp's router can serve: cached from Hugging Face or in its GGUF folder. */
export interface LlamaModel {
  name: string;
  state: "loaded" | "loading" | "unloaded" | "sleeping" | "downloading" | "incomplete" | string;
  loaded: boolean;
  /** The last load failed. */
  failed: boolean;
  path: string;
  size: number | null;
  /** The context the model was trained with, when llama-server says. */
  context_train?: number | null;
  parameters?: number | null;
  progress: { completed: number; total: number } | null;
}

export interface LlamaState extends ProviderBase {
  online: boolean;
  error?: string;
  /** Router mode: it serves many models and can change them. A llama-server run with one model (-m) can't. */
  router: boolean;
  models: LlamaModel[];
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
  /** How many models the machine last counted there, when it says. */
  models: number | null;
  /** For an OpenAI-compatible server (type "api"): which one it is. */
  flavor: ApiFlavor | null;
}

/** Which OpenAI-compatible server a machine found (type "api"). */
export type ApiFlavor = "lmstudio" | "vllm" | "sglang" | "openai";

export const API_FLAVORS: Record<string, string> = { lmstudio: "LM Studio", vllm: "vLLM", sglang: "SGLang", openai: "OpenAI-compatible server" };

/** The model servers on a machine: what it reports running, and the backends that are its (containers among them), by kind and port. */
export function machineProviders(machine: FleetMachine, backends: Record<string, Backend> | undefined): MachineProvider[] {
  const found = new Map<string, MachineProvider>();
  for (const item of machine.providers) {
    const id = `${item.type}-${item.port}`;
    found.set(id, { id, kind: item.type, port: item.port, url: item.url, backend: item.backend, container: false, detected: true, version: item.version || null,
      models: item.models ?? null, flavor: item.flavor ?? null });
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
      models: current?.models ?? null, flavor: current?.flavor ?? null,
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

/** How llama.cpp pulls: "" its router (else agent-machine, when the router is too old), "router" only, or "direct" (agent-machine downloads the files). */
export type PullMethod = "" | "router" | "direct";

/** One file of a llama.cpp model, as agent-machine would download it into its GGUF folder. */
export interface LlamaFile {
  repo_path: string;
  /** Where it goes, inside the GGUF folder. */
  path: string;
  url: string;
  size: number;
  sha256: string;
}

/** What downloading a llama.cpp model by hand means: its files and the GGUF folder agent-machine's llama-server serves. */
export interface LlamaFilesPlan {
  model: string;
  name: string;
  folder: string;
  dir: string;
  files: LlamaFile[];
  /** Whether this llama-server is agent-machine's own, which serves that folder. */
  serves_folder: boolean;
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

/** Whether this browser may watch and type into agents' terminals: only when signed in. local and paired are always false (pairing was removed). */
export interface RemoteStatus {
  trusted: boolean;
  local: false;
  paired: false;
  accounts: true;
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

/** GET /api/auth/accounts: the accounts one browser is signed in to (names only), and which is active. */
export type SignedInAccounts = {
  active: string | null;
  accounts: { user: string; active: boolean }[];
  /** How many one browser keeps signed in at once. */
  max: number;
};

/** A setup code as the server lists it: times are seconds since the epoch; uses is how many sign-ins it has left. */
export interface SetupCode {
  id: string;
  label: string;
  created: number;
  exp: number;
  uses: number;
  max_uses: number;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

/** Empty keeps local, bundled and Android deployments on the page's own origin. */
export const API_ORIGIN = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");

export function apiUrl(path: string): string {
  return `${API_ORIGIN}${path}`;
}

// Access tokens last minutes. The console renews its access cookie this long before it runs out (or at
// RENEW_AT of its lifetime, if sooner), so open streams and terminals reconnect onto a valid cookie and
// requests don't meet a 401 first.
const RENEW_AHEAD_MS = 60_000;
const RENEW_AT = 0.8;
// After a renewal that failed for want of the server (not a refused sign-in), try again this soon.
const RENEW_RETRY_MS = 15_000;
// Every tab's last renewal, so the others don't spend the refresh token again: { at, expiresAt } in ms.
const RENEWED_KEY = "mesh-access-renewed";
const LEASE_KEY = "mesh-access-renewing";

type Renewal = { at: number; expiresAt: number };

function lastRenewal(): Renewal | null {
  try {
    const value = JSON.parse(localStorage.getItem(RENEWED_KEY) ?? "null");
    return value && typeof value.at === "number" && typeof value.expiresAt === "number" ? value : null;
  } catch {
    return null;
  }
}

function noteRenewal(expiresIn: number | null | undefined): number | null {
  if (typeof expiresIn !== "number" || !Number.isFinite(expiresIn)) return null;
  const now = Date.now();
  const expiresAt = now + expiresIn * 1000;
  try {
    localStorage.setItem(RENEWED_KEY, JSON.stringify({ at: now, expiresAt }));
  } catch {
    // Private windows and blocked storage: tabs then each renew for themselves.
  }
  return expiresAt;
}

function forgetRenewal() {
  scheduleRenewal(null);
  try {
    localStorage.removeItem(RENEWED_KEY);
  } catch {
    // Nothing kept.
  }
}

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Runs one renewal at a time across this browser's tabs. Web Locks need a secure context, so a console
 * on plain HTTP (a LAN address) falls back to a lease in localStorage: without either, tabs renewing
 * together would present one refresh token several times, and the issuer revokes the whole sign-in then.
 */
async function oneAtATime<T>(run: () => Promise<T>): Promise<T> {
  if (typeof navigator !== "undefined" && navigator.locks) return navigator.locks.request("mesh-oidc-refresh", run);
  const me = Math.random().toString(36).slice(2);
  const owner = () => {
    try {
      return JSON.parse(localStorage.getItem(LEASE_KEY) ?? "null") as { owner: string; until: number } | null;
    } catch {
      return null;
    }
  };
  let held = false;
  for (const giveUp = Date.now() + 10_000; Date.now() < giveUp; ) {
    const lease = owner();
    if (!lease || lease.until < Date.now()) {
      try {
        localStorage.setItem(LEASE_KEY, JSON.stringify({ owner: me, until: Date.now() + 10_000 }));
      } catch {
        break;
      }
      // Two tabs can write at once; whoever reads its own name back afterwards has it.
      await pause(40);
      if (owner()?.owner === me) {
        held = true;
        break;
      }
    }
    await pause(100 + Math.random() * 150);
  }
  try {
    return await run();
  } finally {
    if (held && owner()?.owner === me) {
      try {
        localStorage.removeItem(LEASE_KEY);
      } catch {
        // Expires by itself.
      }
    }
  }
}

let refreshing: Promise<Response> | null = null;

// Open connections that renew in place: terminal sockets send the new token ({"type":"token"}), live
// streams switch to a new stream on the new cookie. Kept in memory only, never in storage.
const renewedListeners = new Set<(token: string | null) => void>();

/** Calls back after every renewal of this browser's access cookie (this tab's or, when it sees it, another's). */
export function onRenewed(listener: (token: string | null) => void): () => void {
  renewedListeners.add(listener);
  return () => renewedListeners.delete(listener);
}

function announceRenewal(token: unknown) {
  const value = typeof token === "string" && token ? token : null;
  for (const listener of [...renewedListeners]) {
    try {
      listener(value);
    } catch {
      // One connection's trouble isn't the others'.
    }
  }
}

/** Another tab renewed the shared cookie: learn its token from the server, which reads it from the cookie. */
async function adoptRenewal() {
  try {
    const response = await fetch(apiUrl("/api/auth/status"), { credentials: "include" });
    if (response.ok) announceRenewal((await response.json())?.access_token);
  } catch {
    // The connections fall back to reconnecting when their token runs out.
  }
}

/**
 * Renews the active account's access cookie. since (ms): skip it if another tab renewed after then and
 * the cookie has a while left, since the browser shares that cookie; 401 handlers pass when their request
 * was sent. Every successful renewal schedules the next one.
 */
async function refreshSignIn(since = Date.now()): Promise<Response> {
  if (!refreshing) {
    const renew = async () => {
      const last = lastRenewal();
      if (last && last.at > since && last.expiresAt - Date.now() > RENEW_AHEAD_MS) {
        scheduleRenewal(last.expiresAt);
        void adoptRenewal();
        return new Response(JSON.stringify({ refreshed: true }), { status: 200 });
      }
      const response = await fetch(apiUrl("/api/auth/refresh"), { method: "POST", credentials: "include" });
      if (response.ok) {
        const body = await response.clone().json().catch(() => ({}));
        scheduleRenewal(noteRenewal(body?.expires_in));
        announceRenewal(body?.access_token);
      }
      return response;
    };
    refreshing = oneAtATime(renew).finally(() => {
      refreshing = null;
    });
  }
  return refreshing;
}

let renewalTimer: ReturnType<typeof setTimeout> | undefined;
let renewalDue: number | null = null;

/** Renews ahead of expiresAt (ms since the epoch); null stops renewing (signed out, or not known). */
export function scheduleRenewal(expiresAt: number | null) {
  clearTimeout(renewalTimer);
  renewalTimer = undefined;
  renewalDue = null;
  if (expiresAt == null) return;
  const left = expiresAt - Date.now();
  const delay = Math.max(1_000, Math.min(left - RENEW_AHEAD_MS, left * RENEW_AT));
  renewalDue = Date.now() + delay;
  renewalTimer = setTimeout(async () => {
    renewalTimer = undefined;
    renewalDue = null;
    let response: Response | null = null;
    try {
      // A tab that renewed in the last moment did it for this one too.
      response = await refreshSignIn(Date.now() - delay);
    } catch {
      // Unreachable server: below.
    }
    // A refused sign-in (401) stops here; the console's next status check shows sign-in.
    if (!response || (!response.ok && response.status !== 401)) {
      clearTimeout(renewalTimer);
      renewalTimer = setTimeout(() => scheduleRenewal(Date.now()), RENEW_RETRY_MS);
    }
  }, delay);
}

// Background tabs' timers are throttled (to once a minute or less). Coming back, renew at once if it's due.
if (typeof document !== "undefined") {
  const due = () => {
    if (document.visibilityState === "visible" && renewalDue != null && renewalDue <= Date.now()) scheduleRenewal(Date.now());
  };
  document.addEventListener("visibilitychange", due);
  window.addEventListener("online", due);
}

/**
 * Renews the sign-in's access cookie, for connections that can't see a 401 themselves (EventSource,
 * WebSockets). since: when the connection that was refused opened, so a renewal another tab made after
 * that counts. Resolves true if the cookie is fresh; false if it wasn't renewed or the server couldn't
 * be reached.
 */
export async function renewSignIn(since?: number): Promise<boolean> {
  try {
    return (await refreshSignIn(since)).ok;
  } catch {
    return false;
  }
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const sent = Date.now();
  const response = await fetch(apiUrl(path), { ...init, credentials: "include" });
  if (response.status !== 401 || path.startsWith("/api/auth/")) return response;
  const refreshed = await refreshSignIn(sent);
  if (refreshed.ok) return fetch(apiUrl(path), { ...init, credentials: "include" });
  if (refreshed.status !== 401) throw new ApiError("The authentication service is unavailable", refreshed.status);
  return response;
}

/** One message of a conversation sent to a model from the console (Ask a model). */
export interface AskMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

/**
 * Sends a conversation to a model the way an agent would, through the session's OpenAI Chat endpoint,
 * so the server translates, tunes and records it like any agent's call. Calls onText with each piece of
 * the streamed reply (reasoning separately) and resolves when the reply is complete.
 */
export async function askModel(
  sessionId: string,
  body: { model: string; messages: AskMessage[] },
  onText: (piece: { content?: string; reasoning?: string }) => void,
  signal?: AbortSignal,
  onStart?: (requestId: string) => void,
): Promise<void> {
  const response = await apiFetch(`/session/${encodeURIComponent(sessionId)}/v1/chat/completions`, {
    method: "POST",
    // The server reads the reply to the end even if this connection drops, so followReply can pick it up.
    headers: { "content-type": "application/json", "x-ai-proxy-finish": "1" },
    body: JSON.stringify({ ...body, stream: true }),
    signal,
  });
  if (!response.ok || !response.body) throw await failure(response);
  const requestId = response.headers.get("x-request-id");
  if (requestId) onStart?.(requestId);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const events = buffer.split(/\r?\n\r?\n/);
    buffer = events.pop() ?? "";
    for (const event of events) {
      for (const line of event.split(/\r?\n/)) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (!data || data === "[DONE]") continue;
        let chunk: { choices?: { delta?: { content?: string | null; reasoning_content?: string | null; reasoning?: string | null } }[]; error?: unknown };
        try {
          chunk = JSON.parse(data);
        } catch {
          continue;
        }
        if (chunk.error) throw new ApiError(typeof chunk.error === "string" ? chunk.error : JSON.stringify(chunk.error), 502);
        const delta = chunk.choices?.[0]?.delta;
        if (!delta) continue;
        const reasoning = delta.reasoning_content ?? delta.reasoning ?? "";
        if (delta.content || reasoning) onText({ content: delta.content ?? undefined, reasoning: reasoning || undefined });
      }
    }
  }
}

/**
 * Picks a reply back up after askModel's connection dropped: the server went on reading it (askModel
 * asks it to), and /api/live has its text so far and the rest as it comes. Calls onSoFar once with the
 * reply's text up to now (from the newest request of the session started at or after `since`, in ms),
 * then onText with each later piece; resolves when the reply ends. Rejects if there's nothing to follow.
 */
export function followReply(
  sessionId: string,
  requestId: string,
  onSoFar: (text: { content: string; reasoning: string }) => void,
  onText: (piece: { content?: string; reasoning?: string }) => void,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const key = `${sessionId}:${requestId}`;
    let following = false;
    let finished = false;
    let quiet: ReturnType<typeof setTimeout>;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let source: EventSource;
    let unrenewed = () => {};
    const finish = (error?: Error) => {
      if (finished) return;
      finished = true;
      unrenewed();
      clearTimeout(quiet);
      clearTimeout(retry);
      source.close();
      signal?.removeEventListener("abort", aborted);
      if (error) reject(error);
      else resolve();
    };
    // A reply that says nothing for this long (no text, no keep-alive) has gone. Reopening after a
    // closed connection doesn't reset it, so the retries below stop with it.
    const awake = () => {
      clearTimeout(quiet);
      quiet = setTimeout(() => finish(new Error("The reply stopped arriving")), 60_000);
    };
    const aborted = () => finish(new DOMException("Stopped", "AbortError"));
    signal?.addEventListener("abort", aborted);
    awake();
    type Entry = { key: string; content: string; reasoning: string };
    let attempt = 0;
    let opened = 0;
    const open = () => {
      opened = Date.now();
      source = new EventSource(apiUrl(`/api/live?session_id=${encodeURIComponent(sessionId)}`), { withCredentials: true });
      listen(source);
    };
    // The server ends a stream when the token it opened with runs out. After a renewal, move to a new
    // stream on the new cookie first: its snapshot carries the reply so far, so nothing is lost.
    unrenewed = onRenewed(() => {
      if (finished || source?.readyState === EventSource.CLOSED) return;
      source.close();
      open();
    });
    // EventSource reconnects by itself after a dropped connection (each time with a new snapshot), but
    // gives up for good when a reconnect is refused (401 after the access token expired, 502 or 503
    // during a restart). Renew the sign-in and open a new one, a little later each time.
    const reopen = async () => {
      source.close();
      const delay = LIVE_RETRY_MS[Math.min(attempt, LIVE_RETRY_MS.length - 1)];
      attempt += 1;
      await renewSignIn(opened);
      if (finished) return;
      retry = setTimeout(() => {
        if (!finished) open();
      }, delay);
    };
    const listen = (source: EventSource) => {
      source.addEventListener("open", () => {
        attempt = 0;
      });
      source.addEventListener("snapshot", (event) => {
        awake();
        const snapshot = JSON.parse((event as MessageEvent).data) as { enabled: boolean; active: Entry[]; recent: Entry[] };
        // Without the live view's text (AI_PROXY_LIVE=0) there's nothing to pick up.
        if (!snapshot.enabled) return finish(new Error("The server doesn't keep replies' text in flight"));
        const active = snapshot.active.find((entry) => entry.key === key);
        const ended = active ? undefined : snapshot.recent.find((entry) => entry.key === key);
        const entry = active ?? ended;
        if (!entry) return finish(new Error("The reply is no longer on the server"));
        // Its text so far replaces what arrived before the drop (the server keeps the last 24,000 characters).
        onSoFar({ content: entry.content, reasoning: entry.reasoning });
        following = true;
        if (ended) finish();
      });
      source.addEventListener("delta", (event) => {
        awake();
        const delta = JSON.parse((event as MessageEvent).data) as { key: string; kind: string; text: string };
        if (!following || delta.key !== key || !delta.text) return;
        if (delta.kind === "content") onText({ content: delta.text });
        else if (delta.kind === "reasoning") onText({ reasoning: delta.text });
      });
      source.addEventListener("end", (event) => {
        if ((JSON.parse((event as MessageEvent).data) as { key: string }).key === key) finish();
      });
      source.addEventListener("error", () => {
        if (source.readyState === EventSource.CLOSED && !finished) void reopen();
      });
    };
    open();
  });
}

/** How long to wait before each new attempt at a live connection the browser gave up on, in ms. */
export const LIVE_RETRY_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(url, {
    ...init,
    headers: init?.body ? { "content-type": "application/json", ...init.headers } : init?.headers,
  });
  if (!response.ok) throw await failure(response);
  // 204: nothing to read (a delete).
  return (response.status === 204 ? undefined : response.json()) as Promise<T>;
}

/** A plain-text answer, such as a YAML document. */
async function text(url: string): Promise<string> {
  const response = await apiFetch(url);
  if (!response.ok) throw await failure(response);
  return response.text();
}

async function failure(response: Response): Promise<ApiError> {
  let message = `Request failed (${response.status})`;
  try {
    const body = await response.json();
    if (body?.detail) message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch {
    // Keep the status-based message.
  }
  return new ApiError(message, response.status);
}

/** One page of the documentation (docs/<name>.md on the server), as Markdown. Public. */
async function helpPage(name: string): Promise<string> {
  const response = await apiFetch(`/help-files/${encodeURIComponent(name)}.md`);
  if (!response.ok) throw new ApiError(`not found (${response.status})`, response.status);
  return response.text();
}

const sessionUrl = (id: string) => `/api/sessions/${encodeURIComponent(id)}`;
const accountUrl = (id: string) => `/api/provider-accounts/${encodeURIComponent(id)}`;

export const api = {
  /** recent: also the last 100 request records (the Overview's); without, only counts and backends. */
  status: (recent = false) => request<Status>(recent ? "/api/status" : "/api/status?recent=0"),
  /** The server computer's CPU, memory and disks (site admins only). */
  serverStats: () => request<ServerStats>("/api/server/stats"),
  /** Every HTTP request and WebSocket the server received, newest first (site admins only). */
  accessLog: (options: { failed?: boolean; q?: string; limit?: number; before?: number } = {}) => {
    const params = new URLSearchParams();
    if (options.failed) params.set("failed", "true");
    if (options.q) params.set("q", options.q);
    if (options.limit) params.set("limit", String(options.limit));
    if (options.before) params.set("before", String(options.before));
    return request<AccessLog>(`/api/server/access-log?${params}`);
  },
  /** The access log's numbers: since the server started, and from what's saved. */
  accessLogStats: () => request<AccessLogStats>("/api/server/access-log/stats"),
  /** What's saved across restarts (failures, dropped WebSockets, fleet connections), newest first; before is a time. */
  accessLogHistory: (options: { failed?: boolean; fleet?: boolean; q?: string; limit?: number; before?: number } = {}) => {
    const params = new URLSearchParams();
    if (options.failed) params.set("failed", "true");
    if (options.fleet) params.set("fleet", "true");
    if (options.q) params.set("q", options.q);
    if (options.limit) params.set("limit", String(options.limit));
    if (options.before) params.set("before", String(options.before));
    return request<{ entries: AccessEntry[]; kept_days: number }>(`/api/server/access-log/history?${params}`);
  },
  /** Every request saved in the request log, newest first (site admins only); before is a time. */
  requestLog: (options: RequestLogFilters & { limit?: number; before?: number } = {}) => {
    const params = new URLSearchParams();
    for (const key of ["q", "method", "status", "user"] as const) if (options[key]) params.set(key, options[key] ?? "");
    if (options.limit) params.set("limit", String(options.limit));
    if (options.before) params.set("before", String(options.before));
    return request<{ entries: RequestLogEntry[]; settings: RequestLogSettings }>(`/api/server/request-log?${params}`);
  },
  /** One saved request, with its payloads when debug mode kept them. */
  requestLogEntry: (id: string) => request<RequestLogEntry & { payloads: RequestLogPayloads | null }>(`/api/server/request-log/${encodeURIComponent(id)}`),
  requestLogSummary: () => request<RequestLogSummary>("/api/server/request-log/summary"),
  /** Change any of the request log's settings; the answer has them all. */
  saveRequestLog: (changes: Partial<RequestLogSettings>) =>
    request<StorageInfo>("/api/config/storage", { method: "PUT", body: JSON.stringify({ request_log: changes }) }),
  health: () => request<Health>("/health", { cache: "no-store" }),
  helpPage,
  // Served outside /api (public, for machines that aren't signed in yet); the console's page is /get.
  downloads: () => request<DownloadIndex>("/downloads/index.json"),
  sessions: () => request<Session[]>("/api/sessions"),
  session: (sessionId: string) => request<Session>(sessionUrl(sessionId)),
  /** Request records, newest first: limit and before (the oldest timestamp so far) page back through them. */
  requests: (query: RequestQuery = {}) => request<RequestRecord[]>(`/api/requests${queryString(query)}`),
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
  /** Stops the session's replies the server goes on reading for a client that dropped (Ask a model's Stop). */
  stopReplies: (sessionId: string) => request<{ stopped: number }>(`${sessionUrl(sessionId)}/stop`, { method: "POST", body: "{}" }),
  endSession: (sessionId: string, init?: { keepalive?: boolean }) =>
    request<Session>(sessionUrl(sessionId), { method: "PATCH", body: JSON.stringify({ ended: true }), ...init }),
  clearSession: (sessionId: string) => request<Session>(`${sessionUrl(sessionId)}/data`, { method: "DELETE" }),
  deleteSession: (sessionId: string) => request<{ deleted: string }>(sessionUrl(sessionId), { method: "DELETE" }),
  deleteAllSessions: () => request<{ deleted: number; cleared: number; skipped: string[] }>("/api/sessions", { method: "DELETE" }),
  telemetryUrl: (sessionId: string) => `${sessionUrl(sessionId)}/telemetry`,
  agents: () => request<Agent[]>("/api/agents"),
  sharedMcp: () => request<SharedMcp[]>("/api/mcp"),
  intercept: () => request<InterceptInfo>("/api/intercept"),
  prepareAgent: (agentId: string, body: { model: string; backend: string; trace: boolean; project?: string; mcp?: string[] }) =>
    request<PreparedSession>(`/api/agents/${encodeURIComponent(agentId)}/sessions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  models: () => request<ModelsResponse>("/api/models"),
  config: () => request<ConfigResponse>("/api/config"),
  saveConfig: (config: NetworkConfig) =>
    request<ConfigResponse>("/api/config", { method: "PUT", body: JSON.stringify(config) }),
  // The stored settings beyond the network ones (site admins). Definitions travel as YAML text (JSON is YAML too).
  storedAgents: () => request<StoredAgent[]>("/api/config/agents"),
  saveAgent: (id: string, yaml: string) =>
    request<StoredAgent>(`/api/config/agents/${encodeURIComponent(id)}`, { method: "PUT", body: yaml, headers: { "content-type": "application/yaml" } }),
  deleteAgent: (id: string) => request<void>(`/api/config/agents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  resetAgent: (id: string) => request<StoredAgent>(`/api/config/agents/${encodeURIComponent(id)}/reset`, { method: "POST" }),
  modelTuning: () => request<ModelTuning>("/api/config/models"),
  modelTuningYaml: () => text("/api/config/models?format=yaml"),
  saveModelTuning: (yaml: string) =>
    request<ModelTuning>("/api/config/models", { method: "PUT", body: yaml, headers: { "content-type": "application/yaml" } }),
  // OpenAI- and Anthropic-compatible APIs (site admins). Keys go in write-only and never come back.
  apiProviders: (org: string) => request<ApiProvidersResponse>(`/api/orgs/${encodeURIComponent(org)}/providers`),
  saveApiProvider: (org: string, name: string, body: ApiProviderSave) =>
    request<ApiProvider>(`/api/orgs/${encodeURIComponent(org)}/providers/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteApiProvider: (org: string, name: string) =>
    request<{ ok: boolean }>(`/api/orgs/${encodeURIComponent(org)}/providers/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testApiProvider: (org: string, name: string) =>
    request<ApiProviderTest>(`/api/orgs/${encodeURIComponent(org)}/providers/${encodeURIComponent(name)}/test`, { method: "POST" }),
  apiProviderBalance: (org: string, name: string) =>
    request<ApiProviderBalance>(`/api/orgs/${encodeURIComponent(org)}/providers/${encodeURIComponent(name)}/balance`, { method: "POST" }),
  storage: () => request<StorageInfo>("/api/config/storage"),
  saveRetention: (days: number | null) =>
    request<StorageInfo>("/api/config/storage", { method: "PUT", body: JSON.stringify({ retention_days: days }) }),
  // What's recorded and for how long (retention.py): the server's policy (site admins), each organization's, legal holds.
  retentionPolicy: () => request<ServerRetention>("/api/config/retention"),
  saveRetentionPolicy: (policy: ServerRetention) =>
    request<ServerRetention>("/api/config/retention", { method: "PUT", body: JSON.stringify(policy) }),
  orgRetention: (org: string) => request<OrgRetention>(`/api/orgs/${encodeURIComponent(org)}/retention`),
  /** Replaces the organization's own overrides with these (only what it changes). */
  saveOrgRetention: (org: string, own: OrgRetentionOwn) =>
    request<OrgRetention>(`/api/orgs/${encodeURIComponent(org)}/retention`, { method: "PUT", body: JSON.stringify(own) }),
  resetOrgRetention: (org: string) => request<OrgRetention>(`/api/orgs/${encodeURIComponent(org)}/retention`, { method: "DELETE" }),
  /** Admins get their own organization's; site admins every one, or org's. */
  holds: (org?: string) => request<{ holds: LegalHold[] }>(`/api/holds${org ? `?${new URLSearchParams({ org })}` : ""}`),
  addHold: (hold: { scope: HoldScope; target: string; reason: string; org?: string }) =>
    request<LegalHold>("/api/holds", { method: "POST", body: JSON.stringify(hold) }),
  removeHold: (id: string) => request<{ removed: string }>(`/api/holds/${encodeURIComponent(id)}`, { method: "DELETE" }),
  // The audit log (audit.py): admins read their organization's, site admins everyone's.
  audit: (query: AuditQuery = {}) => request<AuditPage>(`/api/audit${auditQuery(query)}`),
  /** The same events as a file (up to 5000), with the server's filename. */
  auditExport: async (query: AuditQuery, format: "csv" | "ndjson") => {
    const response = await apiFetch(`/api/audit/export${auditQuery({ ...query, limit: undefined, before: undefined }, { format })}`);
    if (!response.ok) throw await failure(response);
    return { text: await response.text(), type: response.headers.get("content-type") ?? "text/plain", filename: `audit.${format}` };
  },
  auditVerify: (org?: string) => request<AuditVerify>(`/api/audit/verify${org ? `?${new URLSearchParams({ org })}` : ""}`),
  auditCheckpoint: () => request<{ checkpoint: AuditCheckpoint | null }>("/api/audit/checkpoint", { method: "POST", body: "{}" }),
  auditConfig: () => request<AuditConfig>("/api/config/audit"),
  saveAuditConfig: (config: Pick<AuditConfig, "exports" | "checkpoint_minutes">) =>
    request<AuditConfig>("/api/config/audit", { method: "PUT", body: JSON.stringify(config) }),
  importSettings: (yaml: string) =>
    request<{ changed: string[]; restart_required: boolean }>("/api/config/import", { method: "POST", body: yaml, headers: { "content-type": "application/yaml" } }),
  // LLM providers: everything goes through the machine that runs the model server.
  machineMetrics: (machine: string) => request<MachineSample>(`${machineUrl(machine)}/metrics`),
  ollamaSettings: (machine: string) => request<OllamaSettings>(`${machineUrl(machine)}/servers/ollama`),
  saveOllamaSettings: (machine: string, config: OllamaConfig) =>
    request<OllamaSettings>(`${machineUrl(machine)}/servers/ollama`, { method: "PUT", body: JSON.stringify(config) }),
  llamaSettings: (machine: string) => request<LlamaSettings>(`${machineUrl(machine)}/servers/llamacpp`),
  saveLlamaSettings: (machine: string, config: OllamaConfig) =>
    request<LlamaSettings>(`${machineUrl(machine)}/servers/llamacpp`, { method: "PUT", body: JSON.stringify(config) }),
  llamaInstallOptions: (machine: string) => request<LlamaInstallOptions>(`${machineUrl(machine)}/servers/llamacpp/install`),
  providerStatus: <T extends ProviderBase>(ref: ProviderRef) => request<T>(providerUrl(ref)),
  /** A llama.cpp model's files on Hugging Face, to download them by hand into the machine's GGUF folder. */
  llamaFiles: (ref: ProviderRef, name: string) =>
    request<LlamaFilesPlan>(`${providerUrl(ref)}/models/files`, { method: "POST", body: JSON.stringify({ name }) }),
  /** llama.cpp's router reads its GGUF folder again (after files were put there by hand). */
  llamaReload: (ref: ProviderRef) => request<{ reloaded: string }>(`${providerUrl(ref)}/models/reload`, { method: "POST", body: "{}" }),
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
  /** open: only terminals still running (the header's count and alerts), not every ended one kept. */
  terminals: (open = false) => request<{ terminals: TerminalSummary[] }>(open ? "/api/terminals?open=1" : "/api/terminals"),
  machines: () => request<{ machines: FleetMachine[]; removed?: RemovedMachine[]; features?: Record<MachineFeature, string> }>("/api/machines"),
  setMachineAccess: (machineId: string, access: MachineAccess) =>
    request<FleetMachine>(`/api/machines/${encodeURIComponent(machineId)}/access`, { method: "PUT", body: JSON.stringify({ access }) }),
  restoreMachine: (machineId: string) => request<{ restored: string }>(`/api/machines/${encodeURIComponent(machineId)}/restore`, { method: "POST", body: "{}" }),
  providerAccounts: (machine?: string) =>
    request<ProviderAccountList>(`/api/provider-accounts${machine ? `?machine=${encodeURIComponent(machine)}` : ""}`),
  providerAccount: (id: string) => request<ProviderAccount>(accountUrl(id)),
  createProviderAccount: (body: { provider: SubscriptionProvider; display_name?: string; plan_name?: string; authentication_mode?: "runner" | "token" }) =>
    request<ProviderAccount>("/api/provider-accounts", { method: "POST", body: JSON.stringify(body) }),
  updateProviderAccount: (
    id: string,
    body: Partial<Pick<ProviderAccount, "display_name" | "plan_name" | "priority" | "users" | "status" | "warning_percent" | "stop_percent">> & {
      limit?: DeclaredLimit | null;
      clear_observations?: boolean;
    },
  ) => request<ProviderAccount>(accountUrl(id), { method: "PATCH", body: JSON.stringify(body) }),
  deleteProviderAccount: (id: string) => request<{ deleted: string }>(accountUrl(id), { method: "DELETE" }),
  providerAccountEvents: (id: string) => request<{ events: AccountEvent[] }>(`${accountUrl(id)}/events`),
  assignProviderAccount: (id: string, machine: string, body: { enabled?: boolean; provisioning_allowed?: boolean } = {}) =>
    request<AccountAssignment>(`${accountUrl(id)}/assignments/${encodeURIComponent(machine)}`, { method: "PUT", body: JSON.stringify(body) }),
  unassignProviderAccount: (id: string, machine: string) =>
    request<{ unassigned: string }>(`${accountUrl(id)}/assignments/${encodeURIComponent(machine)}`, { method: "DELETE" }),
  /** Store a managed token (encrypted on the server; it can never be read back). */
  storeProviderToken: (id: string, token: string) =>
    request<ProviderAccount>(`${accountUrl(id)}/credential`, { method: "PUT", body: JSON.stringify({ token }) }),
  revokeProviderToken: (id: string) => request<ProviderAccount>(`${accountUrl(id)}/revoke`, { method: "POST", body: "{}" }),
  planTask: (body: { agents?: string[]; machine?: string; project?: string; fallback?: string[]; allow_unknown?: boolean }) =>
    request<SchedulePlan>("/api/provider-accounts/plan", { method: "POST", body: JSON.stringify(body) }),
  launchOnMachine: (machineId: string, body: LaunchRequest) =>
    request<FleetLaunch & { account?: { id: string; display_name: string; provider: SubscriptionProvider } | null }>(`/api/machines/${encodeURIComponent(machineId)}/launch`, { method: "POST", body: JSON.stringify(body) }),
  machineFolders: (machineId: string, path?: string) =>
    request<MachineFolders>(`/api/machines/${encodeURIComponent(machineId)}/folders${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  machineFiles: (machineId: string, path?: string) =>
    request<MachineFiles>(`/api/machines/${encodeURIComponent(machineId)}/files${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  machineFileContent: (machineId: string, path: string) =>
    request<MachineFileContent>(`/api/machines/${encodeURIComponent(machineId)}/files/content?path=${encodeURIComponent(path)}`),
  updateMachine: (machineId: string, agent: boolean) =>
    request<{ updating: boolean; from: string | null; to: string | null; message: string | null }>(`/api/machines/${encodeURIComponent(machineId)}/update`, {
      method: "POST",
      body: JSON.stringify({ agent }),
    }),
  openShell: (machineId: string, project: string) =>
    request<FleetLaunch>(`/api/machines/${encodeURIComponent(machineId)}/shell`, { method: "POST", body: JSON.stringify({ project }) }),
  adoptProvider: (machineId: string, type: string) =>
    request<{ backend: string; url: string }>(`/api/machines/${encodeURIComponent(machineId)}/providers/adopt`, { method: "POST", body: JSON.stringify({ type }) }),
  /** Start or stop a machine's model server; profile names a Strata profile, variant how to install llama.cpp. */
  machineServer: (machineId: string, kind: "ollama" | "strata" | "llamacpp", action: ServerActionName, profile?: string, variant?: string) =>
    request<{ servers: MachineServers }>(`/api/machines/${encodeURIComponent(machineId)}/servers/${kind}/${action}`, {
      method: "POST",
      body: JSON.stringify(profile ? { profile } : variant ? { variant } : {}),
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
  authStatus: async () => {
    const sent = Date.now();
    let status = await request<AuthStatus>("/api/auth/status");
    if (!status.identity) {
      const refreshed = await refreshSignIn(sent);
      if (refreshed.status !== 401 && !refreshed.ok) throw new ApiError("The authentication service is unavailable", refreshed.status);
      if (refreshed.ok) status = await request<AuthStatus>("/api/auth/status");
    }
    // The server says how long the cookie has left: renew a little before then (this follows renewals
    // made by other tabs too, since they share the cookie).
    if (status.identity && typeof status.expires_in === "number") scheduleRenewal(sent + status.expires_in * 1000);
    else if (!status.identity) scheduleRenewal(null);
    return status;
  },
  login: () => window.location.assign(apiUrl(`/api/auth/login?return_to=${encodeURIComponent(window.location.origin)}`)),
  /** Sign in to another account in this browser: the issuer's sign-in page, then back here with it active. */
  addAccount: () => window.location.assign(apiUrl(`/api/auth/login?return_to=${encodeURIComponent(window.location.origin)}`)),
  /** The accounts this browser is signed in to, and which is active. */
  accounts: () => request<SignedInAccounts>("/api/auth/accounts"),
  /** Make another of this browser's accounts the active one (no password: it's signed in already). */
  switchAccount: async (user: string) => {
    const switched = await request<SignedInAccounts & { expires_in?: number }>("/api/auth/switch", { method: "POST", body: JSON.stringify({ user }) });
    scheduleRenewal(noteRenewal(switched.expires_in));
    return switched;
  },
  /** Sign out of the active account, one named account, or every one ({ all: true }). The next one left becomes active. */
  logout: async (which: { user?: string; all?: boolean } = {}) => {
    const result = await request<SignedInAccounts & { identity: null }>("/api/auth/logout", { method: "POST", body: JSON.stringify(which) });
    // The cookie it described may be gone; the next status check says what's left.
    forgetRenewal();
    return result;
  },
  /** Your open setup codes (never the codes themselves). */
  apiCredentials: (org: string) => request<{ credentials: ApiCredential[]; token_endpoint: string }>(`/api/orgs/${encodeURIComponent(org)}/api-credentials`),
  createApiCredential: (org: string, body: { user: string; label: string; bootstrap: boolean }) =>
    request<{ client_id: string; client_secret: string }>(`/api/orgs/${encodeURIComponent(org)}/api-credentials`, { method: "POST", body: JSON.stringify(body) }),
  regenerateApiCredential: (org: string, id: string) =>
    request<{ client_id: string; client_secret: string }>(`/api/orgs/${encodeURIComponent(org)}/api-credentials/${encodeURIComponent(id)}/regenerate`, { method: "POST" }),
  revokeApiCredential: (org: string, id: string) =>
    request(`/api/orgs/${encodeURIComponent(org)}/api-credentials/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setupCodes: () => request<{ codes: SetupCode[] }>("/api/auth/setup-codes"),
  /** A setup code: machines that sign in with it are yours, without approving each. The answer holds the code, once. */
  createSetupCode: (body: { lifetime: number; uses: number; label?: string }) =>
    request<SetupCode & { code: string }>("/api/auth/setup-codes", { method: "POST", body: JSON.stringify(body) }),
  revokeSetupCode: (id: string) => request<{ revoked: string }>(`/api/auth/setup-codes/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** What an `agent login`/`agent-machine login` code is (check), or decide it, as whoever is signed in. */
  checkDevice: (userCode: string) =>
    request<DeviceRequest>("/api/auth/device", { method: "POST", body: JSON.stringify({ user_code: userCode, action: "check" }) }),
  decideDevice: (userCode: string, approve: boolean) =>
    request<{ approved: boolean }>("/api/auth/device", {
      method: "POST",
      body: JSON.stringify({ user_code: userCode, action: approve ? "approve" : "deny" }),
    }),
  orgs: () => request<{ orgs: Org[] }>("/api/orgs"),
  providers: () => request<ProvidersResponse>("/api/providers"),
  orgUsage: (org: string) => request<OrgUsage>(`/api/orgs/${encodeURIComponent(org)}/usage`),
  setUsageLimit: (org: string, limit: Pick<UsageLimit, "user" | "provider" | "window" | "tokens">) =>
    request<OrgUsage>(`/api/orgs/${encodeURIComponent(org)}/limits`, { method: "PUT", body: JSON.stringify(limit) }),
  removeUsageLimit: (org: string, limit: Pick<UsageLimit, "user" | "provider" | "window">) =>
    request<OrgUsage>(`/api/orgs/${encodeURIComponent(org)}/limits?${new URLSearchParams(limit)}`, { method: "DELETE" }),
  // Links between servers (site admins) and bridges between organizations (their admins); see the peering types below.
  links: () => request<LinksReport>("/api/links"),
  setMeshId: (id: string) => request<LinksReport>("/api/links/mesh", { method: "PUT", body: JSON.stringify({ id }) }),
  linkCode: () => request<LinkCode>("/api/links/codes", { method: "POST", body: "{}" }),
  inspectLink: (url: string) => request<LinkPeer>("/api/links/inspect", { method: "POST", body: JSON.stringify({ url }) }),
  joinLink: (join: { url?: string; link?: string; code: string; fingerprint?: string }) =>
    request<LinkInfo>("/api/links/join", { method: "POST", body: JSON.stringify(join) }),
  link: (id: string) => request<LinkDetail>(`/api/links/${encodeURIComponent(id)}`),
  setLinkPolicy: (id: string, policy: { out: string[]; in: string[]; orgs: string[] }) =>
    request<LinkPolicy>(`/api/links/${encodeURIComponent(id)}/policy`, { method: "PUT", body: JSON.stringify(policy) }),
  linkAction: (id: string, action: "pause" | "resume") =>
    request<LinkInfo>(`/api/links/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" }),
  removeLink: (id: string) => request<{ removed: boolean }>(`/api/links/${encodeURIComponent(id)}`, { method: "DELETE" }),
  rotateLinkKey: () => request<LinksReport>("/api/links/keys/rotate", { method: "POST", body: "{}" }),
  orgBridges: (org: string) => request<OrgBridges>(`/api/orgs/${encodeURIComponent(org)}/bridges`),
  bridgeCode: (org: string, link: string) =>
    request<BridgeCode>(`/api/orgs/${encodeURIComponent(org)}/bridges/codes`, { method: "POST", body: JSON.stringify({ link }) }),
  inspectBridge: (org: string, link: string, code: string) =>
    request<{ org: string; name: string; server: string }>(`/api/orgs/${encodeURIComponent(org)}/bridges/inspect`, {
      method: "POST", body: JSON.stringify({ link, code }),
    }),
  joinBridge: (org: string, join: { link: string; code: string; peer_org: string }) =>
    request<BridgeInfo>(`/api/orgs/${encodeURIComponent(org)}/bridges/join`, { method: "POST", body: JSON.stringify(join) }),
  bridge: (org: string, id: string) => request<BridgeDetail>(`/api/orgs/${encodeURIComponent(org)}/bridges/${encodeURIComponent(id)}`),
  setBridgePolicy: (org: string, id: string, policy: { out: BridgeItem[]; in: BridgeItem[] }) =>
    request<BridgeInfo>(`/api/orgs/${encodeURIComponent(org)}/bridges/${encodeURIComponent(id)}/policy`, { method: "PUT", body: JSON.stringify(policy) }),
  bridgeAction: (org: string, id: string, action: "pause" | "resume" | "refresh") =>
    request<BridgeInfo>(`/api/orgs/${encodeURIComponent(org)}/bridges/${encodeURIComponent(id)}/${action}`, { method: "POST", body: "{}" }),
  endBridge: (org: string, id: string) =>
    request<{ ended: boolean }>(`/api/orgs/${encodeURIComponent(org)}/bridges/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setBridgeLimit: (org: string, id: string, limit: { provider: string; window: UsageWindow; tokens: number }) =>
    request<UsageLimit>(`/api/orgs/${encodeURIComponent(org)}/bridges/${encodeURIComponent(id)}/limits`, { method: "PUT", body: JSON.stringify(limit) }),
  removeBridgeLimit: (org: string, id: string, provider: string, window: UsageWindow) =>
    request<{ removed: boolean }>(
      `/api/orgs/${encodeURIComponent(org)}/bridges/${encodeURIComponent(id)}/limits?${new URLSearchParams({ provider, window })}`,
      { method: "DELETE" },
    ),
  peerResources: () => request<PeerResources>("/api/peer/resources"),
  peerFolders: (machine: string, path = "") =>
    request<MachineFolders>(`/api/peer/folders?${new URLSearchParams(path ? { machine, path } : { machine })}`),
  peerLaunch: (launch: { machine: string; agent: string; project: string; model?: string; backend?: string; mcp?: string[] }) =>
    request<{ session_id: string; remote_session: string; backend: string }>("/api/peer/launch", { method: "POST", body: JSON.stringify(launch) }),
  addOrg: (id: string, name: string) => request<Directory>("/api/orgs", { method: "POST", body: JSON.stringify({ id, name }) }),
  renameOrg: (id: string, name: string) => request<Directory>(`/api/orgs/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ name }) }),
  removeOrg: (id: string) => request<Directory>(`/api/orgs/${encodeURIComponent(id)}`, { method: "DELETE" }),
  addTeam: (org: string, id: string, name: string) =>
    request<Directory>(`/api/orgs/${encodeURIComponent(org)}/teams`, { method: "POST", body: JSON.stringify({ id, name }) }),
  removeTeam: (org: string, id: string) => request<Directory>(`/api/orgs/${encodeURIComponent(org)}/teams/${encodeURIComponent(id)}`, { method: "DELETE" }),
  renameTeam: (org: string, id: string, name: string) =>
    request<Directory>(`/api/orgs/${encodeURIComponent(org)}/teams/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ name }) }),
  setTeamMember: (org: string, team: string, user: string, member: boolean) =>
    request<Directory>(`/api/orgs/${encodeURIComponent(org)}/teams/${encodeURIComponent(team)}/members/${encodeURIComponent(user)}`, { method: member ? "PUT" : "DELETE" }),
  users: () => request<{ users: User[] }>("/api/users"),
  /** tenant names the user's organization (an organization admin's own when left out; root for a site admin, admins only). */
  addUser: (body: { name: string; password: string; role: string; tenant?: string; teams: string[] }) =>
    request<Directory>("/api/users", { method: "POST", body: JSON.stringify(body) }),
  updateUser: (name: string, body: { role?: string; orgs?: string[]; teams?: string[]; password?: string }) =>
    request<Directory>(`/api/users/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(body) }),
  removeUser: (name: string) => request<Directory>(`/api/users/${encodeURIComponent(name)}`, { method: "DELETE" }),
  shareSession: (sessionId: string, sharing: { visibility: Visibility; team?: string | null; org?: string | null; access: Sharing["access"] }) =>
    request<Session>(sessionUrl(sessionId), { method: "PATCH", body: JSON.stringify(sharing) }),
  containerLog: async (name: string) => {
    const response = await apiFetch(`/api/containers/${encodeURIComponent(name)}/log?tail=1000`);
    if (!response.ok) throw new ApiError(`Couldn't read the log (${response.status})`, response.status);
    return response.text();
  },
};

/** The browser sends the API host's session cookie on the viewer WebSocket. */
/**
 * The terminal viewer's socket. `from` is the byte offset of the end of the output the viewer already
 * has, so a reconnect can carry on from there instead of replaying everything.
 */
export function terminalSocketUrl(sessionId: string, from?: number | null): string {
  const url = new URL(apiUrl(`/api/terminals/${encodeURIComponent(sessionId)}/view`), location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (from != null) url.searchParams.set("from", String(from));
  return url.href;
}

function searchCatalog<T>(machine: string, catalog: Catalog, params: Record<string, string>): Promise<T> {
  return request<T>(`${machineUrl(machine)}/catalog/${catalog}?${new URLSearchParams(params)}`);
}

/** Providers whose models can be pulled: Ollama and Docker Model Runner. */
export type PullProvider = "ollama" | "docker";

/**
 * The models to offer for a provider: the ones it has, then the aliases from the server's model tuning
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
 * Add a hosted API (such as Anthropic's) to the server's providers, keeping the others, and
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
  const response = await apiFetch(`${providerUrl(ref)}/log${source ? `?source=${source}` : ""}`);
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
  method: PullMethod = "",
): Promise<void> {
  const response = await apiFetch(`${providerUrl(ref)}/models/pull`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(method ? { name, method } : { name }),
    signal,
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null);
    throw new ApiError(typeof body?.detail === "string" ? body.detail : `Pull failed (${response.status})`, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  // The server ends every pull with a success or an error line; a stream that stops without one
  // was cut off (a dropped network, a restart), and the machine may well still be pulling.
  let succeeded = false;
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
    if (obj.status === "success") succeeded = true;
  };
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new PullConnectionLost();
    }
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let index: number;
    while ((index = buffer.indexOf("\n")) >= 0) {
      consume(buffer.slice(0, index));
      buffer = buffer.slice(index + 1);
    }
  }
  consume(buffer);
  if (!succeeded) throw new PullConnectionLost();
}

/** pullModel lost its connection before the pull finished: it may still be running on the machine. */
export class PullConnectionLost extends Error {
  constructor() {
    super("Lost the connection to the pull. It may still be running on the machine.");
  }
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
/** POST /api/auth/device with action "check": a device code waiting for approval, decided by nobody yet. */
export interface DeviceRequest {
  client_id: string;
  /** In words: "agent or agent-machine" for the command-line client. */
  client: string;
  /** Unix times. requested_at is null for codes from before it was recorded. */
  requested_at: number | null;
  expires_at: number;
  /** Who would be signed in: the person asking. */
  user: string;
}

/** One HTTP request or WebSocket the server received (access_log.py). */
export interface AccessEntry {
  /** A number within this run; a saved entry's own id (a string) in the history. */
  id: number | string;
  /** Read from the database's saved part. */
  saved?: boolean;
  /** Unix time it arrived. */
  time: number;
  kind: "http" | "websocket";
  /** The HTTP method, or WS. */
  method: string;
  path: string;
  /** The query's parameter names only, never their values. */
  query: string;
  client: string;
  forwarded_for: string;
  host: string;
  user_agent: string;
  /** X-AI-Proxy-Version and -Protocol, which agent and agent-machine send. */
  program_version: string;
  protocol: string;
  user: string | null;
  tenant: string | null;
  /** A fleet connection's machine, once its hello named it. */
  machine_id: string | null;
  machine_name: string | null;
  /** The HTTP status; for a WebSocket 101 once accepted, or the status it was refused with. */
  status: number | null;
  close_code: number | null;
  close_reason: string;
  closed_by: "server" | "client" | null;
  /** The start of an error answer's body. */
  detail: string;
  duration_ms: number | null;
  /** dropped: a WebSocket whose connection was lost without a close (a phone asleep or changing network). */
  outcome: "ok" | "failed" | "dropped" | "open";
}

/** A request in the request log: an access log entry, saved, with its own id. */
export type RequestLogEntry = Omit<AccessEntry, "id" | "saved"> & {
  id: string;
  /** Debug mode saved its headers and bodies (they may since have expired). */
  payload: boolean;
};

export interface RequestLogFilters {
  q?: string;
  method?: string;
  /** 2xx, 3xx, 4xx, 5xx, failed or websocket. */
  status?: string;
  user?: string;
}

/** A saved body: JSON or text with secrets redacted, or what it was when it isn't text. */
export interface LoggedBody {
  bytes: number;
  truncated: boolean;
  omitted?: string;
  json?: unknown;
  text?: string;
}

export interface RequestLogPayloads {
  request: { query: string; headers: Record<string, string>; body: LoggedBody };
  response: { headers: Record<string, string>; body: LoggedBody };
}

/** GET /api/server/request-log/summary. */
export interface RequestLogSummary {
  count: number;
  oldest: number | null;
  payloads: number;
  oldest_payload: number | null;
  settings: RequestLogSettings;
}

/** GET /api/server/access-log. */
export interface AccessLog {
  entries: AccessEntry[];
  started_at: number;
  totals: { ok: number; failed: number; dropped: number; open_websockets: number };
  /** How many of all requests, and of the failed ones, the server keeps. */
  kept: { all: number; failed: number };
}

interface AccessLast {
  time: number | null;
  user: string | null;
  user_agent: string;
  program_version: string;
  reason: string;
}

/** GET /api/server/access-log/stats. */
export interface AccessLogStats {
  started_at: number;
  totals: AccessLog["totals"];
  /** The last hour, a minute each (minute: its start, Unix time). */
  per_minute: { minute: number; ok: number; failed: number; dropped: number }[];
  /** How requests were answered: a status, or how a WebSocket ended ("4401 by server"). */
  answers: { answer: string; count: number }[];
  /** Of the newest HTTP answers, in milliseconds. */
  durations: { count: number; p50: number | null; p95: number | null; max: number | null };
  failing_routes: ({ method: string; route: string; answer: string; count: number } & AccessLast)[];
  failing_clients: ({ address: string; count: number } & AccessLast)[];
  open_websockets: { path: string; user: string | null; machine_id: string | null; machine_name: string | null; address: string; since: number; user_agent: string; program_version: string }[];
  saved: {
    days: { day: string; ok: number; failed: number; dropped: number }[];
    /** Each machine's fleet connections, by its sign-in and address, newest first. */
    fleet: {
      /** Null for connections refused before their hello: grouped by sign-in and address instead. */
      machine_id: string | null;
      machine_name: string | null;
      user: string | null;
      address: string;
      connections: number;
      ok: number;
      failed: number;
      dropped: number;
      connected_ms: number;
      last_time: number | null;
      last_answer: string;
      last_reason: string;
      user_agent: string;
      program_version: string;
    }[];
    fleet_days: number;
    oldest: number | null;
    count: number;
    kept_days: number;
  };
}

/** GET /api/server/stats: the computer agent-server runs on, sampled when asked (stats.py). */
export interface ServerStats {
  timestamp: number;
  hostname: string;
  platform: string;
  boot_time: number;
  cpu: { percent: number; count: number; load_average: number[] | null };
  memory: { total: number; used: number; available: number; percent: number };
  swap: { total: number; used: number; percent: number };
  process: { pid: number; rss: number; cpu_percent: number; threads: number; started_at: number };
  /** Each filesystem holding one of the folders below, with their names. */
  disks: { mountpoint: string; total: number; used: number; free: number; percent: number; folders: string[] }[];
  /** bytes is null until first measured (in the background, every few minutes) or when the folder doesn't exist. */
  folders: { name: string; path: string; bytes: number | null }[];
  folders_measured_at: number | null;
  folders_measuring: boolean;
  /** Settings, accounts and every session's records live here; bytes is null until measured. */
  database: { kind: "sqlite" | "postgresql" | null; bytes: number | null };
}

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
  apiFetch(`/api/live/${encodeURIComponent(sessionId)}/${encodeURIComponent(requestId)}`).then(async (response) => {
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

// Peering: links between servers and bridges between organizations (server: links.py, bridges.py).

/** What a link policy lets out or in, by kind. */
export const LINK_KINDS = ["provider", "mcp", "agent", "launch", "terminal"] as const;
export type LinkKind = (typeof LINK_KINDS)[number];

export interface LinkKey {
  kid: string;
  fingerprint: string;
  until: number | null;
}

export interface LinkInfo {
  id: string;
  peer: string;
  url: string;
  state: "pending" | "active" | "paused";
  connected: boolean;
  we_presented: boolean;
  they_presented: boolean;
  /** This side dials the link socket (it reached the other while joining). */
  dials: boolean;
  /** This side's code waits for the other to dial in (it couldn't be reached from here). */
  waiting: boolean;
  fingerprint: string | null;
  keys: LinkKey[];
  created_at: number;
  updated_at: number;
}

export interface LinksReport {
  mesh: { id: string | null; url: string; fingerprint: string; link_protocol: number; keys: LinkKey[] };
  links: LinkInfo[];
}

export interface LinkCode {
  code: string;
  expires_at: number;
  mesh_id: string;
  url: string;
  fingerprint: string;
}

export interface LinkPeer {
  mesh_id: string;
  url: string;
  fingerprint: string;
  link_protocol: number;
}

export interface LinkPolicy {
  out: LinkKind[];
  in: LinkKind[];
  orgs: string[];
  version?: number;
}

export interface LinkDetail extends LinkInfo {
  policy: LinkPolicy;
  peer_policy: LinkPolicy;
  bridges: BridgeInfo[];
}

/** One thing a bridge policy lets out or in. scope "home": only for its own people's launches on the other server. */
export interface BridgeItem {
  kind: LinkKind;
  name?: string;
  machine?: string;
  digest?: string;
  scope?: "home" | null;
  teams?: string[];
}

export interface BridgeCatalogue {
  server?: string;
  org?: string;
  providers?: { name: string; type: string; scope?: string | null; protocols: string[]; models: string[]; connected: boolean }[];
  mcp?: { machine: string; name: string; description?: string; scope?: string | null; connected: boolean }[];
  agents?: { name: string; digest: string }[];
  machines?: { name: string; connected: boolean; os?: string; functions: LinkKind[]; agents: string[]; roots: string[]; launches?: boolean }[];
}

export interface BridgeInfo {
  id: string;
  link: string;
  server: string | null;
  org: string;
  peer_org: string;
  state: "pending" | "active" | "paused";
  we_confirmed: boolean;
  they_confirmed: boolean;
  link_state: string;
  connected: boolean;
  policy: { out: BridgeItem[]; in: BridgeItem[]; version?: number | null };
  peer_policy: { out: BridgeItem[]; in: BridgeItem[]; version?: number | null };
  crossing: { out: BridgeItem[]; in: BridgeItem[] };
  catalogue: BridgeCatalogue;
  refreshed_at?: number | null;
  link_policy: { local: Partial<LinkPolicy>; peer: Partial<LinkPolicy> };
}

export interface BridgeDetail extends BridgeInfo {
  limits: UsageLimit[];
  shareable: {
    providers: { name: string; type: string }[];
    agents: { name: string; display: string }[];
    machines: { name: string; id: string; granted: string[]; connected: boolean; mcp: string[] }[];
  };
}

export interface OrgBridges {
  bridges: BridgeInfo[];
  links: { id: string; server: string; connected: boolean; lets_out: LinkKind[]; lets_in: LinkKind[] }[];
  codes: { id: string; link: string | null; expires_at: number; created_by: string | null }[];
  mesh_id: string | null;
}

export interface BridgeCode {
  code: string;
  expires_at: number;
  org: string;
  server: string;
}

/** What other servers' organizations let in for you, each under its full name (id). */
export interface PeerResources {
  providers: { id: string; name: string; type: string; protocols: string[]; models: string[]; connected: boolean; bridge: string }[];
  mcp: { id: string; machine: string; name: string; description?: string; connected: boolean; bridge: string }[];
  agents: { id: string; name: string; digest: string; bridge: string }[];
  machines: {
    id: string;
    name: string;
    connected: boolean;
    os?: string;
    functions: LinkKind[];
    agents: string[];
    roots: string[];
    launches?: boolean;
    bridge: string;
  }[];
}
