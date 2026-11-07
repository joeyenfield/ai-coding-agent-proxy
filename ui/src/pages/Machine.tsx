import { lazy, Suspense, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, NavLink, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, API_FLAVORS, machineProviders, preferredModel, providerModels, type Agent, type FleetMachine, type MachineProvider, type ProviderRef, type StrataProfile } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { FolderPicker } from "../components/FolderPicker";
import { Icon } from "../components/Icon";
import { ShareChoice } from "../components/Sharing";
import { StateDot } from "../components/ProviderState";
import { pollEvery, useAgents, useAuth, useClipboard, useMachines, useModels, useSessions, useStatus, useToast } from "../hooks";
import { providerLabel } from "../routes";
import { formatRelative } from "../format";
import { OllamaProvider } from "./Models";
import { StrataProvider } from "./Strata";
import { ApiServerProvider } from "./ApiProviders";
import { DockerGuide, DockerProvider } from "./Docker";
import { MachineOllama } from "./OllamaServer";
import { LlamaCppProvider, MachineLlamaCpp } from "./LlamaCpp";
import { MachineLoad } from "./Hosts";
import { terminalState } from "./Terminals";
import { MachineAccess } from "./MachineAccess";
import { MachineMcp } from "./MachineMcp";
import { MachineAccounts } from "./MachineAccounts";
import { MachineFiles } from "./MachineFiles";
import { STATE_LABELS } from "./Subscriptions";
import { McpChoice } from "../components/McpChoice";

// Containers are rarely open; load them when visited.
const MachineContainers = lazy(() => import("./Containers").then((module) => ({ default: module.MachineContainers })));

// The model select's last choice: type a name instead.
const OTHER_MODEL = "__another__";

export const OS_LABELS: Record<string, string> = { windows: "Windows", linux: "Linux", darwin: "macOS", freebsd: "FreeBSD" };

/** A model server's name on its machine's page: "Ollama :11434", with its backend's name once it's one. */
export function providerName(provider: MachineProvider): string {
  const label = provider.kind === "api" && provider.flavor ? API_FLAVORS[provider.flavor] ?? providerLabel(provider.kind) : providerLabel(provider.kind);
  return `${label} :${provider.port}`;
}

/** A machine's page, or one of its tabs ("agents", or "providers/ollama/ollama-11434": each part encoded). */
export const machinePath = (id: string, section?: string) =>
  `/machines/${encodeURIComponent(id)}${section ? `/${section.split("/").map(encodeURIComponent).join("/")}` : ""}`;

/** The machine's Providers tab: its overview, a kind's tab ("ollama", "containers"), or one instance or the kind's server tab ("server"). */
export const providersPath = (id: string, kind?: string, sub?: string) => machinePath(id, ["providers", kind, sub].filter(Boolean).join("/"));

/**
 * One machine of the fleet, and everything it runs for the server: its model servers (each
 * with its models, settings and logs), the Ollama and Strata installed there, its containers,
 * its load, and starting agents on it. All of it is asked of the machine itself.
 */
export function MachinePage() {
  const { id = "", section = "", kind = "", sub = "" } = useParams();
  const machines = useMachines();
  const status = useStatus();
  const machine = machines.data?.machines.find((item) => item.id === id);
  const providers = useMemo(() => (machine ? machineProviders(machine, status.data?.backends) : []), [machine, status.data]);

  if (machines.isPending) return <div className="page"><p className="empty">Reading the fleet…</p></div>;
  if (machines.isError) return <div className="page"><p className="bad">{machines.error.message}</p></div>;
  if (!machine) {
    return (
      <div className="page">
        <p className="empty">
          No machine {id} here. It may have been forgotten, or it's another user's. See <Link to="/machines">Machines</Link>.
        </p>
      </div>
    );
  }
  // The tabs that used to be on their own are in Providers now: old links still land on them.
  const moved = movedSection(machine, section);
  if (moved) return <Navigate to={moved} replace />;

  return (
    <div className="page wide">
      <MachineHeader machine={machine} />
      <nav className="machine-nav" aria-label={`${machine.name} sections`}>
        <NavLink to={machinePath(machine.id)} end>
          Summary
        </NavLink>
        <NavLink to={machinePath(machine.id, "agents")}>Agents</NavLink>
        <NavLink to={machinePath(machine.id, "terminal")}>Terminal</NavLink>
        <NavLink to={machinePath(machine.id, "files")}>Files</NavLink>
        <NavLink to={providersPath(machine.id)}>
          Providers
          {providers.length > 0 && <span className="nav-note">{providers.length}</span>}
        </NavLink>
        <NavLink to={machinePath(machine.id, "load")}>Load</NavLink>
        {(machine.you.owner || (machine.mcp?.servers?.length ?? 0) > 0) && <NavLink to={machinePath(machine.id, "mcp")}>MCP servers</NavLink>}
        <NavLink to={machinePath(machine.id, "accounts")}>Agent accounts</NavLink>
        {machine.you.manage && <NavLink to={machinePath(machine.id, "access")}>Access</NavLink>}
      </nav>

      {section === "" && <MachineSummary machine={machine} providers={providers} />}
      {section === "agents" && <MachineAgents machine={machine} />}
      {section === "terminal" && <MachineTerminal machine={machine} />}
      {section === "files" && <MachineFiles machine={machine} />}
      {section === "providers" && <MachineProviders machine={machine} providers={providers} kind={kind} sub={sub} />}
      {section === "access" && <MachineAccess machine={machine} />}
      {section === "mcp" && <MachineMcp machine={machine} />}
      {section === "accounts" && <MachineAccounts machine={machine} />}
      {section === "load" && (
        <MachineLoad
          machine={machine.id}
          ollama={providers.filter((item) => item.kind === "ollama").map((item) => ({ target: refOf(machine, item), label: item.backend ?? providerName(item) }))}
        />
      )}
      {section && !SECTIONS.includes(section) && (
        <p className="empty">
          {machine.name} has no tab {section}. See its <Link to={machinePath(machine.id)}>summary</Link>.
        </p>
      )}
    </div>
  );
}

const refOf = (machine: FleetMachine, provider: MachineProvider): ProviderRef => ({ machine: machine.id, provider: provider.id });

// The machine's tabs (its summary is the page itself).
const SECTIONS = ["agents", "terminal", "files", "providers", "load", "mcp", "accounts", "access"];

/** Why the machine won't change its model servers now, or null when it will. */
function unmanageable(machine: FleetMachine): string | null {
  if (!machine.connected) return `${machine.name} isn't connected. Start agent-machine there (agent-machine start) to manage it.`;
  if (!machine.servers.enabled) {
    return `Changing models from here is turned off on ${machine.name}, so you can look but not pull, delete, load or unload. To turn it on, run "agent-machine servers on" in a terminal on ${machine.name}. That also lets this console start and stop its model servers. It takes effect within a minute, with no restart.`;
  }
  return null;
}

function ProviderView({ machine, provider }: { machine: FleetMachine; provider: MachineProvider }) {
  const target = refOf(machine, provider);
  const label = provider.backend ?? `${providerName(provider)} on ${machine.name}`;
  const manageable = unmanageable(machine);
  return (
    <>
      <ProviderHead machine={machine} provider={provider} />
      <OfflineServer machine={machine} provider={provider} />
      {provider.kind === "ollama" && <OllamaProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
      {provider.kind === "llamacpp" && <LlamaCppProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
      {provider.kind === "strata" && <StrataProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
      {provider.kind === "docker" && <DockerProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
      {provider.kind === "api" && <ApiServerProvider provider={provider} />}
    </>
  );
}

/** A model server's title on its machine: what it is, where, and whether agents can use it yet. */
function ProviderHead({ machine, provider }: { machine: FleetMachine; provider: MachineProvider }) {
  return (
    <div className="provider-head">
      <h2>
        {provider.backend ?? providerName(provider)}
        {provider.backend && <span className="subtle"> {providerName(provider)}</span>}
      </h2>
      <p className="subtle">
        <span className="mono">{provider.url}</span>
        {provider.container && " · a container this machine runs for the server"}
        {provider.version && ` · ${provider.version}`}
      </p>
      {!provider.backend && <AdoptButton machine={machine} provider={provider} />}
    </div>
  );
}

function AdoptButton({ machine, provider }: { machine: FleetMachine; provider: MachineProvider }) {
  const client = useQueryClient();
  const notify = useToast();
  const identity = useAuth().data?.identity;
  // Admins of the machine's organization add its model servers as providers (site admins don't reach other organizations).
  const admin = Boolean(identity && identity.role === "admin" && (!machine.tenant || machine.tenant === identity.tenant));
  const adopt = useMutation({
    mutationFn: () => api.adoptProvider(machine.id, provider.kind),
    onSuccess: (result) => {
      notify(`Added the provider ${result.backend}. Agents can use it now.`);
      client.invalidateQueries();
    },
    onError: (error: Error) => notify(error.message),
  });
  if (!admin) return <p className="callout">Agents can use it once an admin of its organization adds it as a provider.</p>;
  return (
    <div className="callout row wrap">
      <span>Agents can't use it yet. Add it as a provider and every agent that speaks its API can.</span>
      <button type="button" className="primary small" disabled={adopt.isPending || !machine.connected} onClick={() => adopt.mutate()}>
        {adopt.isPending ? "Adding…" : "Add as a provider"}
      </button>
    </div>
  );
}

/** Whether you can start an agent on the machine now: connected, launches on there, a folder, an agent, and you're let in. */
export function launchReady(machine: FleetMachine, installed: Agent[]): boolean {
  return machine.connected && machine.launches && Boolean(machine.you.launch) && machine.roots.length > 0 && installed.length > 0 && machine.launcher !== false;
}

/** Whether you can open a terminal on the machine now. */
function shellReady(machine: FleetMachine): boolean {
  return machine.connected && Boolean(machine.commands) && Boolean(machine.you.terminal) && machine.roots.length > 0 && machine.launcher !== false;
}

function MachineHeader({ machine }: { machine: FleetMachine }) {
  const agents = useAgents();
  const installed = (agents.data ?? []).filter((agent) => machine.agents.includes(agent.id));
  return (
    <>
      <header className="page-head machine-head">
        <p className="part-tag part-machines">
          <Link to="/machines">Machines</Link>
          <span className="part-sep" aria-hidden="true">›</span>
          {machine.name}
        </p>
        <div className="machine-title">
          <span className="provider-icon" aria-hidden="true">
            <Icon name="monitor" size={22} />
          </span>
          <div>
            <h1>
              {machine.name}{" "}
              <span className={`pill ${machine.connected ? "good" : ""}`}>
                <StateDot tone={machine.connected ? "good" : "idle"} />{" "}
                {machine.connected ? "Online" : `Seen ${formatRelative(new Date(machine.last_seen * 1000).toISOString())}`}
              </span>
              {machine.update?.available && <span className="pill live">Update available</span>}
            </h1>
            <p className="subtle">
              {OS_LABELS[machine.os] ?? machine.os} {machine.arch}
              {machine.owner && <> · {machine.owner}</>}
              {machine.address && <> · <span className="mono">{machine.address}</span></>}
              {machine.version && <> · agent-machine {machine.version}</>}
            </p>
          </div>
          <div className="row wrap machine-card-actions">
            {launchReady(machine, installed) && (
              <Link className="button primary" to={machinePath(machine.id, "agents")}>
                <Icon name="play" size={14} />
                Start an agent
              </Link>
            )}
            {shellReady(machine) && (
              <Link className="button" to={machinePath(machine.id, "terminal")}>
                <Icon name="terminal" size={14} />
                Open a terminal
              </Link>
            )}
          </div>
        </div>
      </header>
      {machine.update?.available && <UpdateNote machine={machine} update={machine.update} />}
    </>
  );
}

/** The machine's agent-machine is another version than the server's: how to bring it level. */
function UpdateNote({ machine, update }: { machine: FleetMachine; update: NonNullable<FleetMachine["update"]> }) {
  const copy = useClipboard();
  const notify = useToast();
  const client = useQueryClient();
  const [agent, setAgent] = useState(false);
  const remote = machine.connected && Boolean(machine.updates) && Boolean(machine.you.update);
  const start = useMutation({
    mutationFn: () => api.updateMachine(machine.id, agent),
    onSuccess: (result) => {
      notify(result.updating ? `Updating ${machine.name} to ${result.to}; it reconnects on the new version within a minute.` : (result.message ?? `${machine.name} is up to date.`));
      client.invalidateQueries({ queryKey: ["machines"] });
    },
    onError: (error: Error) => notify(error.message),
  });
  return (
    <div className="callout machine-update">
      <p>
        {machine.name} runs agent-machine {machine.version || "of an unknown version"}; this server is {update.server_version}.
        {remote ? " Update it from here to this server's build:" : " Update it there, from this server's build (it restarts itself if it runs in the background):"}
      </p>
      {remote ? (
        <>
          <label className="check">
            <input type="checkbox" checked={agent} onChange={(event) => setAgent(event.target.checked)} />
            Update agent there too (it stops agent-proxy, so agents running there lose their connection until the next one starts)
          </label>
          <div className="row wrap">
            <button type="button" className="primary" disabled={start.isPending} onClick={() => start.mutate()}>
              <Icon name="download" size={14} />
              {start.isPending ? "Asking…" : "Update now"}
            </button>
            <span className="subtle">
              It installs this server's build there, checked against its SHA-256, and restarts. One run in a terminal there has to be
              restarted by hand.
            </span>
          </div>
        </>
      ) : (
        <>
          <CodeLine text={update.command} onCopy={() => copy(update.command, "Command copied")} />
          <p className="subtle">
            Or download it from <Link to="/get">Downloads</Link>.
            {machine.you.update && machine.connected && (
              <>
                {" "}Run <code>agent-machine updates on</code> there to update it from here next time.
              </>
            )}
          </p>
        </>
      )}
    </div>
  );
}

/** One of the machine's features on its summary: whether the machine has it on, and whether you can use it. */
function Feature({ name, on, detail, you, tab, machine }: {
  name: string;
  on: boolean;
  detail: ReactNode;
  you?: boolean;
  tab?: string;
  machine: FleetMachine;
}) {
  return (
    <li>
      <StateDot tone={on ? "good" : "idle"} />
      <span className="feature-name">
        {tab ? <Link to={machinePath(machine.id, tab)}>{name}</Link> : <strong>{name}</strong>}
      </span>
      <span className="feature-detail">{detail}</span>
      <span className="feature-you subtle">{you === undefined ? "" : you ? "You can use it" : "Not for you"}</span>
    </li>
  );
}

/** The first tab: what the machine is, what it supports, and when it was last in touch. */
function MachineSummary({ machine, providers }: { machine: FleetMachine; providers: MachineProvider[] }) {
  const agents = useAgents();
  const sessions = useSessions();
  const installed = (agents.data ?? []).filter((agent) => machine.agents.includes(agent.id));
  const running = (sessions.data ?? []).filter((session) => !session.ended_at && (session.tags?.machine === machine.id || session.machine === machine.id)).length;
  const seen = new Date(machine.last_seen * 1000);
  const first = machine.first_seen ? new Date(machine.first_seen * 1000) : null;
  const off = (command: string) => (
    <span className="subtle">
      Off. Run <code>{command}</code> there.
    </span>
  );
  return (
    <div className="machine-overview">
      <dl className="stat-cards machine-stats">
        <div>
          <dt>Status</dt>
          <dd>{machine.connected ? "Online" : "Offline"}</dd>
        </div>
        <div>
          <dt>Last contact</dt>
          <dd title={seen.toLocaleString()}>{machine.connected ? "Now" : formatRelative(seen.toISOString())}</dd>
        </div>
        <div>
          <dt>agent-machine</dt>
          <dd className="download-version" title={machine.update?.available ? `This server is ${machine.update.server_version}` : undefined}>
            {machine.version || "Unknown"}
            {machine.update?.available && <small> update available</small>}
          </dd>
        </div>
        <div>
          <dt>Running agents</dt>
          <dd>{running}</dd>
        </div>
      </dl>

      <section className="section" aria-labelledby="machine-features">
        <div className="section-head">
          <h2 id="machine-features">What it supports</h2>
          <p className="subtle">What {machine.name} lets this server do, and whether you can. Each switch is on the machine itself.</p>
        </div>
        <ul className="feature-list">
          <Feature
            machine={machine}
            name="Agents"
            tab="agents"
            on={machine.launches}
            you={machine.you.launch}
            detail={
              machine.launches
                ? `${installed.length} of the server's agents installed, in ${machine.roots.length} launch ${machine.roots.length === 1 ? "folder" : "folders"}`
                : off("agent-machine launches on")
            }
          />
          <Feature machine={machine} name="Terminals" tab="terminal" on={Boolean(machine.commands)} you={machine.you.terminal} detail={machine.commands ? "A shell there, for whoever it lets in" : off("agent-machine commands on")} />
          <Feature machine={machine} name="Files" tab="files" on={Boolean(machine.files)} you={machine.you.files} detail={machine.files ? "Browse and read the files in its launch folders" : off("agent-machine files on")} />
          <Feature
            machine={machine}
            name="Model servers"
            tab="providers"
            on={providers.length > 0}
            you={machine.you.models}
            detail={
              <>
                {providers.length ? providers.map((provider) => provider.backend ?? providerName(provider)).join(", ") : "None found"}
                {providers.length > 0 && !machine.servers.enabled && <span className="subtle"> · models can't be changed from here (agent-machine servers on)</span>}
              </>
            }
          />
          <Feature
            machine={machine}
            name="Running Ollama, llama.cpp and Strata"
            tab={machine.servers.ollama?.found ? "providers/ollama/server" : undefined}
            on={Boolean(machine.servers.enabled)}
            you={machine.you.servers}
            detail={machine.servers.enabled ? "Start, stop and configure the ones installed there, and install llama.cpp" : off("agent-machine servers on")}
          />
          <Feature
            machine={machine}
            name="Containers"
            tab={machine.containers.enabled ? "providers/containers" : undefined}
            on={Boolean(machine.containers.enabled)}
            you={machine.you.containers ?? machine.you.owner}
            detail={machine.containers.enabled ? `${machine.containers.cli ?? "docker"}${machine.containers.online ? "" : ", not answering"} · its owner runs them, if an admin of its organization` : off("agent-machine containers on")}
          />
          <Feature machine={machine} name="Updates from here" on={Boolean(machine.updates)} you={machine.you.update} detail={machine.updates ? "Update now, when it's on another build than this server" : off("agent-machine updates on")} />
          <Feature machine={machine} name="Tunnel" on={Boolean(machine.tunnel)} detail={machine.tunnel ? "Agents reach its model servers through its own connection: no port forward needed" : "Agents reach its model servers at its address"} />
        </ul>
      </section>

      <section className="section" aria-labelledby="machine-details">
        <div className="section-head">
          <h2 id="machine-details">Details</h2>
        </div>
        <dl className="machine-facts">
          <div>
            <dt>Owner</dt>
            <dd>{machine.owner ?? <span className="subtle">None (no accounts)</span>}</dd>
          </div>
          <div>
            <dt>System</dt>
            <dd>
              {OS_LABELS[machine.os] ?? machine.os} {machine.arch}
            </dd>
          </div>
          <div>
            <dt>Computer name</dt>
            <dd>{machine.hostname || <span className="subtle">Not said</span>}</dd>
          </div>
          <div>
            <dt>Address</dt>
            <dd className="mono">{machine.address || "Unknown"}</dd>
          </div>
          <div>
            <dt>First seen</dt>
            <dd>{first ? `${formatRelative(first.toISOString())} (${first.toLocaleDateString()})` : "Unknown"}</dd>
          </div>
          <div>
            <dt>Last contact</dt>
            <dd>{seen.toLocaleString()}</dd>
          </div>
          <div>
            <dt>Protocol</dt>
            <dd>{machine.protocol ?? <span className="subtle">Not said</span>}</dd>
          </div>
          <div>
            <dt>Machine id</dt>
            <dd className="mono">{machine.id}</dd>
          </div>
        </dl>
      </section>

      {machine.you.manage && <RemoveMachine machine={machine} />}
    </div>
  );
}

/** Taking the machine out of the fleet: it's disconnected, and refused until allowed back from Machines. */
function RemoveMachine({ machine }: { machine: FleetMachine }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const notify = useToast();
  const [asking, setAsking] = useState(false);
  const remove = useMutation({
    mutationFn: () => api.removeMachine(machine.id),
    onSuccess: () => {
      notify(`Removed ${machine.name} from this server.`);
      client.invalidateQueries({ queryKey: ["machines"] });
      navigate("/machines");
    },
  });
  return (
    <section className="section danger-zone" aria-labelledby="machine-remove">
      <div className="section-head">
        <h2 id="machine-remove">Remove from this server</h2>
      </div>
      <p className="subtle">
        Disconnects {machine.name} and keeps it out: it can't join again until you allow it back on Machines.
        {machine.backends.length > 0 && ` Its providers (${machine.backends.join(", ")}) stop reaching agents meanwhile.`}
      </p>
      <div className="row">
        <button type="button" className="danger" onClick={() => setAsking(true)}>
          <Icon name="trash" size={14} />
          Remove {machine.name}
        </button>
      </div>
      <ConfirmDialog
        open={asking}
        title={`Remove ${machine.name}?`}
        confirmLabel="Remove"
        busy={remove.isPending}
        error={remove.error?.message ?? null}
        onConfirm={() => remove.mutate()}
        onCancel={() => setAsking(false)}
      >
        <p>
          {machine.connected ? "It's connected now: its connection closes, and agents running there lose this server. " : ""}
          It stays out until you allow it back on Machines.
        </p>
      </ConfirmDialog>
    </section>
  );
}

/** Agents: those running there, starting one, and the launches so far. */
function MachineAgents({ machine }: { machine: FleetMachine }) {
  const agents = useAgents();
  const installed = (agents.data ?? []).filter((agent) => machine.agents.includes(agent.id));
  const missing = [
    !machine.connected && "it isn't connected",
    !machine.launches && "launches are off there (agent-machine launches on)",
    machine.launcher === false && "the agent command isn't installed there (install the client, then agent login)",
    machine.roots.length === 0 && "it has no launch folders (agent-machine roots add <dir>)",
    installed.length === 0 && "none of the server's agents is installed there",
  ].filter((reason): reason is string => Boolean(reason));
  return (
    <div className="machine-overview">
      <MachineSessions machine={machine} />
      <section className="section" aria-labelledby="machine-launch">
        <div className="section-head">
          <h2 id="machine-launch">Start an agent</h2>
        </div>
        {!machine.you.launch ? (
          <p className="empty">Only {machine.owner ?? "its owner"}, or the teams it lets (Access), can start agents on {machine.name}.</p>
        ) : launchReady(machine, installed) ? (
          <LaunchForm machine={machine} agents={installed} />
        ) : (
          <p className="empty">You can't start one now: {missing.join("; ")}.</p>
        )}
      </section>
      <section className="section" aria-labelledby="machine-agents">
        <div className="section-head">
          <h2 id="machine-agents">Installed there</h2>
        </div>
        <dl className="machine-facts">
          <div>
            <dt>Agents</dt>
            <dd className="chips">
              {installed.length ? installed.map((agent) => <span key={agent.id} className="pill">{agent.name}</span>) : <span className="subtle">None of the server's agents</span>}
            </dd>
          </div>
          <div>
            <dt>Launch folders</dt>
            <dd className="chips">
              {machine.roots.length ? (
                machine.roots.map((root) => (
                  <span key={root} className="pill mono">
                    {root}
                  </span>
                ))
              ) : (
                <span className="subtle">
                  None yet: <code>agent-machine roots add &lt;dir&gt;</code>
                </span>
              )}
            </dd>
          </div>
        </dl>
      </section>
      <LaunchHistory machine={machine} />
    </div>
  );
}

/** What was started on the machine from here, agents and terminals, newest first. */
function LaunchHistory({ machine }: { machine: FleetMachine }) {
  const agents = useAgents();
  if (machine.history.length === 0) return null;
  return (
    <section className="section" aria-labelledby="machine-history">
      <div className="section-head">
        <h2 id="machine-history">Started from here</h2>
      </div>
      <ul className="launch-history">
        {[...machine.history].reverse().map((launch) => (
          <li key={launch.launch}>
            <span className={launch.error ? "bad" : "good"}>{launch.error ? "Failed" : "Started"}</span>
            <strong>{launch.agent === "shell" ? "Terminal" : agents.data?.find((agent) => agent.id === launch.agent)?.name ?? launch.agent}</strong>
            <span className="mono subtle">{launch.project}</span>
            <span className="subtle">
              {launch.by && `by ${launch.by}, `}
              {formatRelative(new Date(launch.at * 1000).toISOString())}
            </span>
            {launch.session_id ? <Link to={`/terminals/${launch.session_id}`}>Terminal</Link> : <span className="bad">{launch.error}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A terminal on the machine: a shell in one of its launch folders, when it takes commands. */
function MachineTerminal({ machine }: { machine: FleetMachine }) {
  const missing = [
    !machine.connected && "it isn't connected",
    !machine.commands && "terminals are off there (agent-machine commands on)",
    machine.launcher === false && "the agent command isn't installed there (install the client, then agent login)",
    machine.roots.length === 0 && "it has no launch folders to start in (agent-machine roots add <dir>)",
  ].filter((reason): reason is string => Boolean(reason));
  return (
    <div className="machine-overview">
      <section className="section" aria-labelledby="machine-terminal">
        <div className="section-head">
          <h2 id="machine-terminal">Open a terminal</h2>
        </div>
        {!machine.you.terminal ? (
          <p className="empty">Only {machine.owner ?? "its owner"}, or the teams it lets (Access), can open terminals on {machine.name}.</p>
        ) : shellReady(machine) ? (
          <ShellForm machine={machine} />
        ) : (
          <p className="empty">You can't open one now: {missing.join("; ")}.</p>
        )}
      </section>
      <LaunchHistory machine={machine} />
    </div>
  );
}

/** Where each kind of provider comes in the machine's Providers tab. */
const KIND_ORDER = ["ollama", "llamacpp", "strata", "docker", "api"];

/** The kinds of provider the machine has: those it runs, and those installed there (managed from the kind's server tab). */
function providerKinds(machine: FleetMachine, providers: MachineProvider[]): string[] {
  const kinds = new Set(providers.map((item) => item.kind));
  if (machine.servers.ollama?.found) kinds.add("ollama");
  // llama.cpp can be installed from here, so its tab is there whenever the machine knows about it.
  if (machine.servers.enabled && machine.servers.llamacpp?.known) kinds.add("llamacpp");
  if (machine.servers.strata?.found) kinds.add("strata");
  const rank = (kind: string) => (KIND_ORDER.includes(kind) ? KIND_ORDER.indexOf(kind) : KIND_ORDER.length);
  return [...kinds].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

/** The machine-wide tab of a kind (installing, starting and configuring it there), or null when it has none. */
function serverTabLabel(machine: FleetMachine, kind: string): string | null {
  if (kind === "ollama" && machine.servers.ollama?.found) return "Ollama server";
  if (kind === "llamacpp" && machine.servers.enabled && machine.servers.llamacpp?.known) return "llama.cpp server";
  return null;
}

const runsContainers = (machine: FleetMachine, providers: MachineProvider[]) => Boolean(machine.containers.enabled) || providers.some((item) => item.container);

/**
 * The machine's providers in one tab: an overview, then a tab per kind (Ollama, llama.cpp, Strata,
 * Docker Model Runner), each with a tab per instance (a model server or a container) and the
 * kind's server tab, and the containers the machine runs.
 */
function MachineProviders({ machine, providers, kind, sub }: { machine: FleetMachine; providers: MachineProvider[]; kind: string; sub: string }) {
  const kinds = providerKinds(machine, providers);
  return (
    <>
      <nav className="machine-subnav" aria-label={`${machine.name}'s providers`}>
        <NavLink to={providersPath(machine.id)} end>
          Overview
        </NavLink>
        {kinds.map((item) => {
          const count = providers.filter((provider) => provider.kind === item).length;
          return (
            <NavLink key={item} to={providersPath(machine.id, item)}>
              {providerLabel(item)}
              {count > 0 && <span className="nav-note">{count}</span>}
            </NavLink>
          );
        })}
        {runsContainers(machine, providers) && <NavLink to={providersPath(machine.id, "containers")}>Containers</NavLink>}
      </nav>
      {!kind && <MachineModelServers machine={machine} providers={providers} />}
      {kind === "containers" && (
        <Suspense fallback={<p className="subtle">Loading…</p>}>
          <MachineContainers machine={machine.id} />
        </Suspense>
      )}
      {kind && kind !== "containers" && <ProviderKind key={kind} machine={machine} providers={providers} kind={kind} sub={sub} />}
    </>
  );
}

/** One kind of provider on the machine: a tab for each instance, and the kind's server tab. */
function ProviderKind({ machine, providers, kind, sub }: { machine: FleetMachine; providers: MachineProvider[]; kind: string; sub: string }) {
  const instances = providers.filter((item) => item.kind === kind);
  const server = serverTabLabel(machine, kind);
  // Without a choice in the address, the first instance (else the server tab) is open.
  const chosen = sub || instances[0]?.id || (server ? "server" : "");
  const instance = instances.find((item) => item.id === chosen);
  const active = (id: string) => ({ isActive }: { isActive: boolean }) => (isActive || id === chosen ? "active" : "");
  return (
    <>
      {(instances.length > 0 || server) && (
        <nav className="instance-nav" aria-label={`${providerLabel(kind)} on ${machine.name}`}>
          {instances.map((item) => (
            <NavLink key={item.id} to={providersPath(machine.id, kind, item.id)} className={active(item.id)} title={providerName(item)}>
              <StateDot tone={machine.connected && item.detected ? "good" : "idle"} />
              {item.backend ?? `Port ${item.port}`}
              {item.container && <span className="nav-note">container</span>}
            </NavLink>
          ))}
          {server && (
            <NavLink to={providersPath(machine.id, kind, "server")} className={active("server")}>
              {server}
            </NavLink>
          )}
        </nav>
      )}
      {instance && <ProviderView key={instance.id} machine={machine} provider={instance} />}
      {chosen === "server" && kind === "ollama" && <MachineOllama machine={machine} />}
      {chosen === "server" && kind === "llamacpp" && <MachineLlamaCpp machine={machine} />}
      {!instance && !(chosen === "server" && server) && (
        <p className="empty">
          {chosen
            ? `${machine.name} doesn't report ${chosen}. It may have stopped; the overview lists what it runs now.`
            : `${machine.name} runs no ${providerLabel(kind)} now.`}{" "}
          <Link to={providersPath(machine.id)}>Providers overview</Link>
        </p>
      )}
    </>
  );
}

/** Where the old machine tabs (one per model server, Ollama's and llama.cpp's server tabs, containers) are now. */
function movedSection(machine: FleetMachine, section: string): string | null {
  if (section === "model-servers") return providersPath(machine.id);
  if (section === "ollama" || section === "llamacpp") return providersPath(machine.id, section, "server");
  if (section === "containers") return providersPath(machine.id, "containers");
  const provider = /^([a-z]+)-(\d+)$/.exec(section);
  return provider ? providersPath(machine.id, provider[1], section) : null;
}

/** The model servers the machine runs, and the Ollama and Strata installed there. */
function MachineModelServers({ machine, providers }: { machine: FleetMachine; providers: MachineProvider[] }) {
  return (
    <div className="machine-overview">
      <section className="section" aria-labelledby="machine-servers">
        <div className="section-head">
          <h2 id="machine-servers">Providers on this machine</h2>
          <p className="subtle">
            What agents' requests reach on this machine. Open one for its models, status and logs.
            {machine.tunnel && " Their requests go through this machine's own connection to the server, so its model servers need no port forward or open port."}
          </p>
        </div>
        {providers.length ? (
          <ul className="provider-backends">
            {providers.map((provider) => (
              <li key={provider.id}>
                <StateDot tone={machine.connected && provider.detected ? "good" : "idle"} />
                <span className="provider-backend-name">
                  <strong>{providerName(provider)}</strong>
                  {provider.version && <span className="subtle">{provider.version}</span>}
                </span>
                <span className="subtle">
                  {provider.backend ? <>Provider <strong>{provider.backend}</strong></> : "Not a provider yet"}
                  {provider.models != null && ` · ${provider.models} ${provider.models === 1 ? "model" : "models"}`}
                  {provider.container && " · container"}
                  {!provider.detected && " · not answering now"}
                </span>
                <span className="state-text">
                  <Link className="button small" to={providersPath(machine.id, provider.kind, provider.id)}>
                    Open
                  </Link>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty">
            <p>No model server found on {machine.name}.</p>
            <p className="subtle">
              Start Ollama, Strata, Docker Model Runner, LM Studio, vLLM or SGLang there and agent-machine finds it within a minute. Or run{" "}
              <code>agent-machine containers on</code> there so the server can run Strata or Ollama on it in containers.
            </p>
          </div>
        )}
        <InstalledServers machine={machine} />
        {!providers.some((provider) => provider.kind === "docker") && (
          <details className="manual">
            <summary>Docker Model Runner on this machine</summary>
            <DockerGuide />
          </details>
        )}
      </section>
    </div>
  );
}

/** A session open this long with no traffic and no connected terminal is one whose launcher went away without closing it. */
const STALE_MS = 15 * 60 * 1000;

/**
 * The agents running on the machine now: those launched from here and those started there by
 * hand. Each opens its terminal when it shares one (--control, and every launch from here),
 * else its session's requests.
 */
function MachineSessions({ machine }: { machine: FleetMachine }) {
  const sessions = useSessions();
  const agents = useAgents();
  // The rail polls the same list; devices that can't watch terminals get a 401 and see sessions only.
  const terminals = useQuery({ queryKey: ["terminals"], queryFn: () => api.terminals(), retry: false, refetchInterval: pollEvery(3000) });
  const byId = new Map((terminals.data?.terminals ?? []).map((item) => [item.session_id, item]));
  const now = Date.now();
  const running = (sessions.data ?? [])
    .filter((session) => {
      if (session.machine !== machine.id || session.ended_at) return false;
      const terminal = byId.get(session.session_id);
      if (terminal) return !terminal.ended_at && (terminal.connected || now - new Date(terminal.last_output_at ?? session.started_at).getTime() < STALE_MS);
      return now - new Date(session.last_activity_at ?? session.started_at).getTime() < STALE_MS;
    })
    .sort((a, b) => b.started_at.localeCompare(a.started_at));
  return (
    <section className="section" aria-labelledby="machine-sessions">
      <div className="section-head">
        <h2 id="machine-sessions">Running agents</h2>
        <p className="subtle">Agents working on {machine.name} now, launched from here or started there. Open one to watch it and type into it.</p>
      </div>
      {running.length ? (
        <ul className="provider-backends machine-sessions">
          {running.map((session) => {
            const terminal = byId.get(session.session_id);
            const state = terminal ? terminalState(terminal, now) : null;
            const name = agents.data?.find((agent) => agent.id === session.client)?.name ?? session.client;
            const path = String(session.tags.project_path ?? "");
            return (
              <li key={session.session_id} className={state?.tone === "ask" ? "is-asking" : undefined}>
                <StateDot tone={state?.tone ?? "good"} />
                <span className="provider-backend-name">
                  <strong>{name}</strong>
                  <span className="subtle" title={path || undefined}>{session.project ?? path}</span>
                </span>
                <span className="machine-session-state">
                  <span className={state?.tone === "ask" ? "terminal-question" : undefined}>
                    {state ? `${state.label}${state.tone === "ask" ? `: ${state.detail}` : ""}` : "No shared terminal"}
                  </span>
                  <small className="subtle">
                    {[session.model, `${session.request_count} requests`, `started ${formatRelative(session.started_at)}`].filter(Boolean).join(" · ")}
                  </small>
                </span>
                <span className="state-text">
                  {terminal ? (
                    <Link className={`button small ${state?.tone === "ask" ? "primary" : ""}`} to={`/terminals/${encodeURIComponent(session.session_id)}`}>
                      {state?.tone === "ask" ? "Answer" : "Open terminal"}
                    </Link>
                  ) : (
                    <Link className="button small" to={`/sessions/${encodeURIComponent(session.session_id)}`}>
                      Session
                    </Link>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        sessions.isSuccess && (
          <p className="subtle">
            No agents running on {machine.name}.{" "}
            {machine.launches && machine.you.launch ? "Start one with Start an agent above." : "Agents started there with agent show up here while they run."}
          </p>
        )
      )}
    </section>
  );
}

type ServerAction = { kind: "ollama" | "strata" | "llamacpp"; action: "start" | "stop" | "remove" | "prune" | "cleanup"; profile?: string };

const DONE: Record<ServerAction["action"], string> = {
  start: "started",
  stop: "stopped",
  remove: "removed",
  prune: "",
  cleanup: "",
};
const WORKING: Record<ServerAction["action"], string> = {
  start: "Starting…",
  stop: "Stopping…",
  remove: "Removing…",
  prune: "Removing…",
  cleanup: "Cleaning up…",
};

/** Starting and stopping the Ollama or a Strata profile installed on a machine (and tidying Strata's), and the button for it. */
function useServerAction(machine: FleetMachine) {
  const client = useQueryClient();
  const notify = useToast();
  const act = useMutation({
    mutationFn: ({ kind, action, profile }: ServerAction) => api.machineServer(machine.id, kind, action, profile),
    onSuccess: (_, { kind, action, profile }) => {
      const what = kind === "ollama" ? "Ollama" : kind === "llamacpp" ? "llama.cpp" : `Strata ${profile}`;
      notify(
        action === "prune"
          ? `Removed the Strata profiles whose model is gone on ${machine.name}.`
          : action === "cleanup"
            ? `Cleared Strata's leftovers on ${machine.name}.`
            : `${what} ${DONE[action]} on ${machine.name}.`,
      );
      client.invalidateQueries({ queryKey: ["machines"] });
      client.invalidateQueries({ queryKey: ["models"] });
      client.invalidateQueries({ queryKey: ["provider", machine.id] });
    },
    onError: (error: Error) => notify(error.message),
  });
  const allowed = machine.connected && Boolean(machine.servers?.enabled) && Boolean(machine.you.servers);
  const busy = (request: ServerAction) =>
    act.isPending && act.variables?.kind === request.kind && act.variables?.action === request.action && act.variables?.profile === request.profile;
  const button = (request: ServerAction, label: string, primary = false, onClick?: () => void) => (
    <button type="button" className={`small ${primary ? "primary" : ""}`} disabled={!allowed || act.isPending} onClick={onClick ?? (() => act.mutate(request))}>
      {busy(request) ? WORKING[request.action] : label}
    </button>
  );
  return { allowed, button, act };
}

/**
 * Tidying a machine's Strata: Remove for each profile whose model is gone (or all of them at
 * once), and Clean up for what runtimes left behind when their owner went away (a restart).
 * Shown only when the machine's agent-machine says it can.
 */
function StrataTidy({
  machine,
  profiles,
  asking,
  setAsking,
}: {
  machine: FleetMachine;
  profiles: StrataProfile[];
  /** The removal waiting for a yes: one profile's (from its row's Remove) or all old ones. */
  asking: ServerAction | null;
  setAsking: (request: ServerAction | null) => void;
}) {
  const { button, act } = useServerAction(machine);
  const actions = machine.servers?.strata?.actions ?? [];
  const old = profiles.filter((profile) => profile.state === "broken");
  if (!actions.includes("cleanup") && !(actions.includes("prune") && old.length)) return null;
  const confirm = () => {
    if (asking) act.mutate(asking, { onSettled: () => setAsking(null) });
  };
  const prune: ServerAction = { kind: "strata", action: "prune" };
  return (
    <>
      <div className="row wrap strata-tidy">
        {actions.includes("prune") && old.length > 0 &&
          button(prune, old.length === 1 ? "Remove the old profile" : `Remove ${old.length} old profiles`, false, () => setAsking(prune))}
        {actions.includes("cleanup") && button({ kind: "strata", action: "cleanup" }, "Clean up leftovers")}
        <span className="subtle">
          Old profiles are ones whose model was removed or reinstalled in Strata. Clean up ends what Strata left behind when it didn't shut
          down (a restart); it never touches a profile that's serving.
        </span>
      </div>
      <ConfirmDialog
        open={asking !== null}
        title={asking?.action === "remove" ? `Remove Strata profile ${asking.profile}?` : "Remove the old Strata profiles?"}
        confirmLabel="Remove"
        busy={act.isPending}
        error={act.error?.message ?? null}
        onConfirm={confirm}
        onCancel={() => setAsking(null)}
      >
        <p>
          {asking?.action === "remove" ? (
            <>
              This deletes the profile <code>{asking.profile}</code> on {machine.name}, with its history.
            </>
          ) : (
            <>
              This deletes {old.map((profile) => profile.id).join(", ")} on {machine.name}, with their history.
            </>
          )}{" "}
          Their models are already gone, so nothing that runs is lost. Models still installed keep their profiles.
        </p>
      </ConfirmDialog>
    </>
  );
}

/**
 * Above a model server's own tab when the machine doesn't report it running: why, and the way
 * to start it from here when agent-machine can (servers on, and you may manage them).
 */
function OfflineServer({ machine, provider }: { machine: FleetMachine; provider: MachineProvider }) {
  const { allowed, button } = useServerAction(machine);
  if (provider.detected || provider.container || (provider.kind !== "ollama" && provider.kind !== "strata" && provider.kind !== "llamacpp")) return null;
  const servers = machine.servers ?? {};
  const name = provider.kind === "ollama" ? "Ollama" : provider.kind === "llamacpp" ? "llama.cpp" : "Strata";
  let body: ReactNode;
  if (!machine.connected) {
    body = <p>{machine.name} isn't connected, so it can't start {name} now.</p>;
  } else if (!servers.enabled) {
    body = (
      <p>
        {name} isn't running on {machine.name}. Start it there, or run <code>agent-machine servers on</code> there to start it from here.
      </p>
    );
  } else if (provider.kind === "llamacpp") {
    const llama = servers.llamacpp;
    body = llama?.found ? (
      <div className="row wrap">
        <p>llama.cpp is installed on {machine.name} but not running.</p>
        {button({ kind: "llamacpp", action: "start" }, "Start llama.cpp", true)}
      </div>
    ) : (
      <p>
        llama.cpp isn't installed on {machine.name}. Install it from its <Link to={providersPath(machine.id, "llamacpp", "server")}>llama.cpp server</Link> tab.
      </p>
    );
  } else if (provider.kind === "ollama") {
    const ollama = servers.ollama;
    body = ollama?.found ? (
      <div className="row wrap">
        <p>Ollama is installed on {machine.name} but not running.</p>
        {button({ kind: "ollama", action: "start" }, "Start Ollama", true)}
      </div>
    ) : (
      <p>
        Ollama isn't installed on {machine.name}. Install it there from <a href="https://ollama.com/download" target="_blank" rel="noreferrer">ollama.com</a>, then
        start it from here.
      </p>
    );
  } else {
    const strata = servers.strata;
    const stopped = strata?.profiles.filter(canStartStrata) ?? [];
    body = !strata?.found ? (
      <p>
        Strata isn't installed on {machine.name}: agent-machine can't find the <code>strata</code> command there. Install it, or remove this
        provider in Server settings if it's gone for good.
      </p>
    ) : stopped.length ? (
      <div className="row wrap">
        <p>Strata isn't running on {machine.name}. Start one of its profiles:</p>
        {stopped.map((profile) => (
          <span key={profile.id}>{button({ kind: "strata", action: "start", profile: profile.id }, `Start ${profile.id}`, true)}</span>
        ))}
      </div>
    ) : (
      <p>
        {strata.error ??
          (strata.profiles.length
            ? `Strata isn't running on ${machine.name}, and none of its profiles can start from here. See Model servers for why.`
            : `Strata is installed on ${machine.name} but has no model to start: install one in the Strata app there.`)}
      </p>
    );
  }
  return (
    <div className="callout offline-server">
      {body}
      {servers.enabled && machine.connected && !allowed && <p className="subtle">Starting it from here needs the machine's servers permission.</p>}
    </div>
  );
}

/** Whether a Strata profile can be started: stopped, or stale (its runtime's host went away, as when the computer restarted; starting it recovers). */
function canStartStrata(profile: StrataProfile) {
  return profile.state === "stopped" || profile.state === "stale";
}

/** Ollama and Strata installed on the machine, which its owner can start and stop from here (agent-machine servers on). */
function InstalledServers({ machine }: { machine: FleetMachine }) {
  const servers = machine.servers ?? {};
  const { button } = useServerAction(machine);
  const [asking, setAsking] = useState<ServerAction | null>(null);
  if (!servers.enabled) {
    return machine.you.owner ? (
      <p className="subtle">
        To start and stop the Ollama, llama.cpp or Strata installed there, install llama.cpp, tune them, and pull, load and delete models from
        here, run{" "}
        <code>agent-machine servers on</code> there.
      </p>
    ) : null;
  }
  const ollama = servers.ollama;
  const strata = servers.strata;
  const llama = servers.llamacpp;
  return (
    <>
      <h3 className="subhead">Installed there</h3>
      <ul className="provider-backends" aria-label={`Model servers ${machine.name} can start`}>
        <li>
          <StateDot tone={ollama?.running ? "good" : "idle"} />
          <span className="provider-backend-name">
            <strong>Ollama</strong>
            {ollama?.version && <span className="subtle">{ollama.version}</span>}
          </span>
          <span className="subtle">
            {!ollama?.found && !ollama?.running
              ? "Not installed there"
              : ollama.running
                ? ollama.managed
                  ? `Running, started from here${ollama.bind === "0.0.0.0" ? " on every address" : ""}`
                  : "Running, started outside agent-machine"
                : "Installed, not running"}
          </span>
          <span className="state-text">
            {ollama?.found && !ollama.running && button({ kind: "ollama", action: "start" }, "Start", true)}
            {ollama?.managed && button({ kind: "ollama", action: "stop" }, "Stop")}
            {ollama?.found && (
              <Link className="button small" to={providersPath(machine.id, "ollama", "server")}>
                Settings
              </Link>
            )}
          </span>
        </li>
        {llama?.known && (
          <li>
            <StateDot tone={llama.running ? "good" : "idle"} />
            <span className="provider-backend-name">
              <strong>llama.cpp</strong>
              {llama.version && <span className="subtle">{llama.version}</span>}
            </span>
            <span className="subtle">
              {llama.install?.state === "installing"
                ? "Installing…"
                : !llama.found
                  ? "Not installed there: install it from here"
                  : llama.running
                    ? llama.managed
                      ? "Running, started from here"
                      : "Running, started outside agent-machine"
                    : "Installed, not running"}
            </span>
            <span className="state-text">
              {llama.found && !llama.running && button({ kind: "llamacpp", action: "start" }, "Start", true)}
              {llama.managed && llama.running && button({ kind: "llamacpp", action: "stop" }, "Stop")}
              <Link className="button small" to={providersPath(machine.id, "llamacpp", "server")}>
                {llama.found ? "Settings" : "Install"}
              </Link>
            </span>
          </li>
        )}
        {!strata?.found && (
          <li>
            <StateDot tone="idle" />
            <span className="provider-backend-name">
              <strong>Strata</strong>
            </span>
            <span className="subtle">Not installed there</span>
            <span />
          </li>
        )}
        {strata?.found && strata.profiles.length === 0 && (
          <li>
            <StateDot tone="idle" />
            <span className="provider-backend-name">
              <strong>Strata</strong>
            </span>
            <span className="subtle">{strata.error ?? "No model installed: install one in the Strata app there"}</span>
            <span />
          </li>
        )}
        {strata?.profiles.map((profile) => {
          const stopped = canStartStrata(profile);
          return (
            <li key={profile.id}>
              <StateDot tone={stopped || profile.state === "unknown" ? "idle" : "good"} />
              <span className="provider-backend-name">
                <strong>Strata</strong>
                <span className="mono subtle">{profile.id}</span>
                {profile.model && <span className="subtle">{profile.model}</span>}
              </span>
              <span className="subtle" title={profile.error ?? undefined}>
                {profile.state === "broken"
                  ? `Can't start: ${profile.broken}`
                  : profile.state === "stale"
                    ? "Not running: it stopped without shutting down (the computer restarted?)"
                    : stopped ? "Not running" : profile.state === "unknown" ? "State unknown" : `${profile.state[0].toUpperCase()}${profile.state.slice(1)}`}
                {profile.url && <span className="mono"> · {profile.url}</span>}
              </span>
              <span className="state-text">
                {stopped
                  ? button({ kind: "strata", action: "start", profile: profile.id }, "Start", true)
                  : profile.state !== "unknown" && profile.state !== "broken" && button({ kind: "strata", action: "stop", profile: profile.id }, "Stop")}
                {profile.state === "broken" &&
                  strata.actions?.includes("remove") &&
                  button({ kind: "strata", action: "remove", profile: profile.id }, "Remove", false, () =>
                    setAsking({ kind: "strata", action: "remove", profile: profile.id }),
                  )}
              </span>
            </li>
          );
        })}
      </ul>
      {strata?.found && <StrataTidy machine={machine} profiles={strata.profiles} asking={asking} setAsking={setAsking} />}
    </>
  );
}

/** A shell on the machine, in a terminal only you see: agent-machine commands on there. */
function ShellForm({ machine, onDone }: { machine: FleetMachine; onDone?: () => void }) {
  const navigate = useNavigate();
  const notify = useToast();
  const client = useQueryClient();
  const [project, setProject] = useState(machine.roots[0] ?? "");
  const open = useMutation({
    mutationFn: () => api.openShell(machine.id, project),
    onSuccess: (result) => {
      client.invalidateQueries({ queryKey: ["machines"] });
      onDone?.();
      if (result.session_id) navigate(`/terminals/${result.session_id}`);
    },
    onError: (error: Error) => notify(error.message),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    open.mutate();
  };
  return (
    <form className="launch-form panel" onSubmit={submit}>
      <div className="field">
        Start in
        <FolderPicker machine={machine} onChange={setProject} />
      </div>
      <p className="subtle">
        A shell on {machine.name} as the user agent-machine runs as, with what that user can do there. Only you see it, and it
        stays open until you exit it.
      </p>
      <div className="row wrap">
        <button type="submit" className="primary" disabled={open.isPending || !project}>
          {open.isPending ? "Opening…" : `Open a terminal on ${machine.name}`}
        </button>
        {onDone && (
          <button type="button" onClick={onDone}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}

/**
 * Start an agent on a machine (its page, and Start an agent); initialAgent is the one chosen first.
 * fixedAgent: Start an agent has picked it already, so the form doesn't offer the choice again.
 */
export function LaunchForm({ machine, agents, onDone, initialAgent, fixedAgent }: { machine: FleetMachine; agents: Agent[]; onDone?: () => void; initialAgent?: string; fixedAgent?: boolean }) {
  const navigate = useNavigate();
  const notify = useToast();
  const client = useQueryClient();
  const [agentId, setAgentId] = useState(agents.some((item) => item.id === initialAgent) ? initialAgent! : agents[0]?.id ?? "");
  const [project, setProject] = useState(machine.roots[0] ?? "");
  const [model, setModel] = useState("");
  const [backend, setBackend] = useState("");
  const [trace, setTrace] = useState(false);
  const [share, setShare] = useState({ value: "private", control: false });
  const [mcp, setMcp] = useState<string[]>([]);
  const agent = agents.find((item) => item.id === agentId);
  // For account agents: which subscription it runs with. Only the machine's owner may use the machine's own
  // sign-in (""); anyone else picks one of the subscriptions they may use, or lets the server choose.
  const [account, setAccount] = useState<string | null>(null);
  const subscriptions = useQuery({
    queryKey: ["provider-accounts", machine.id],
    queryFn: () => api.providerAccounts(machine.id),
    enabled: Boolean(agent?.subscription),
  });
  const usable = (subscriptions.data?.accounts ?? []).filter((item) => item.you.use && item.provider === agent?.subscription);
  const chosenAccount = account ?? (machine.you.owner ? "" : "auto");
  const status = useStatus();
  const models = useModels();
  // The provider the launch uses: the one picked, else the one the server picks for the agent.
  const backends = (agent?.backends ?? []).filter((name) => status.data?.backends[name]);
  const effective = backend || agent?.default_backend || "";
  const listing = models.data?.backends[effective];
  const available = useMemo(
    () => providerModels(listing, models.data?.aliases, status.data?.backends[effective]?.type),
    [listing, models.data, status.data, effective],
  );
  // "Another model…": a name typed in, for one the provider doesn't list (it may pull it on first use).
  const [typing, setTyping] = useState(false);
  // Whether a model (including "The agent's default", "") was picked by hand: auto-fill must not override it.
  const [chose, setChose] = useState(false);
  // Fill in the provider's model when none was chosen or typed, or when the one there isn't this provider's.
  useEffect(() => {
    if (chose || typing || agent?.intercept || !available.length || available.includes(model)) return;
    setModel(preferredModel(available));
  }, [agent, available, model, typing, chose]);
  const describe = (name: string) => {
    const info = status.data?.backends[name];
    return `${name} (${providerLabel(info?.type, true)}${info?.machine_name ? ` on ${info.machine_name}` : ""})`;
  };
  const launch = useMutation({
    mutationFn: () => {
      const [visibility, team] = share.value.split(":");
      return api.launchOnMachine(machine.id, {
        agent: agentId, project, model: model.trim() || undefined, backend: backend || undefined, trace,
        visibility, team, access: share.control ? "control" : "view", mcp: agent?.mcp ? mcp : undefined,
        account_id: agent?.subscription && chosenAccount ? chosenAccount : undefined,
      });
    },
    onSuccess: (result) => {
      notify(`Started ${agent?.name ?? agentId} on ${machine.name}${result.account ? ` with ${result.account.display_name}` : ""}.`);
      client.invalidateQueries({ queryKey: ["machines"] });
      onDone?.();
      if (result.session_id) navigate(`/terminals/${result.session_id}`);
    },
    onError: (error: Error) => notify(error.message),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    launch.mutate();
  };
  return (
    <form className="launch-form panel" onSubmit={submit}>
      <div className="form-grid">
        {!fixedAgent && (
          <label className="field">
            Agent
            <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
              {agents.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {agent && !agent.intercept && backends.length > 0 && (
          <label className="field">
            Provider
            <select
              value={backend}
              onChange={(event) => {
                setBackend(event.target.value);
                setModel("");
              }}
            >
              <option value="">Default: {agent.default_backend ? describe(agent.default_backend) : "none"}</option>
              {backends.map((name) => (
                <option key={name} value={name}>
                  {describe(name)}
                </option>
              ))}
            </select>
          </label>
        )}
        {agent?.subscription && (
          <label className="field span-2">
            Subscription
            <select value={chosenAccount} onChange={(event) => setAccount(event.target.value)}>
              {machine.you.owner && <option value="">{machine.name}'s own sign-in</option>}
              <option value="auto">Choose automatically: the best one available now</option>
              {usable.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.display_name} ({item.status === "disabled" ? "turned off" : STATE_LABELS[item.state].text.toLowerCase()})
                </option>
              ))}
            </select>
            <small>
              {chosenAccount === ""
                ? `Whatever ${agent.executable} is signed in to on ${machine.name}.`
                : usable.length === 0
                  ? <>None of your subscriptions runs on {machine.name}. Assign one on <Link to="/subscriptions">Subscriptions</Link>.</>
                  : "The provider's own limits apply; one that's throttled or used up isn't chosen."}
            </small>
          </label>
        )}
        <div className="field span-2">
          Folder
          <FolderPicker machine={machine} onChange={setProject} allowNew />
        </div>
        <label className="field span-2">
          <span className="field-label">
            Model {agent?.needs_model ? "" : <span className="optional">optional</span>}
          </span>
          {available.length > 0 && !typing ? (
            <select
              value={model}
              required={agent?.needs_model}
              onChange={(event) => {
                setChose(true);
                if (event.target.value === OTHER_MODEL) {
                  setTyping(true);
                  setModel("");
                } else {
                  setModel(event.target.value);
                }
              }}
            >
              {!agent?.needs_model && <option value="">The agent's default</option>}
              {available.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
              <option value={OTHER_MODEL}>Another model…</option>
            </select>
          ) : (
            <span className="row">
              <input
                value={model}
                required={agent?.needs_model}
                placeholder={agent?.needs_model ? "qwen3.6:35b" : "The agent's default"}
                onChange={(event) => setModel(event.target.value)}
              />
              {available.length > 0 && (
                <button type="button" className="small" onClick={() => { setTyping(false); setModel(preferredModel(available)); }}>
                  Pick from the list
                </button>
              )}
            </span>
          )}
          {!agent?.intercept && effective && (
            <small className={listing && !listing.online ? "bad" : undefined}>
              {models.isPending
                ? `Asking ${effective} for its models…`
                : !listing
                  ? `${effective} hasn't said which models it has.`
                  : !listing.online
                    ? `${effective} is offline, so its models can't be listed: ${listing.error ?? "no answer"}`
                    : available.length
                      ? `${available.length} ${available.length === 1 ? "model" : "models"} on ${effective}, aliases from the model tuning included.`
                      : `${effective} answers, but lists no models yet.`}
            </small>
          )}
        </label>
      </div>
      {agent && <McpChoice agent={agent} chosen={mcp} onChange={setMcp} />}
      <ShareChoice value={share.value} control={share.control} onChange={(value, control) => setShare({ value, control })} />
      <label className="check">
        <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
        Capture full request and response payloads
      </label>
      <div className="row wrap">
        <button type="submit" className="primary" disabled={launch.isPending || !agentId || !project}>
          {launch.isPending ? "Starting…" : `Start on ${machine.name}`}
        </button>
        {onDone && (
          <button type="button" onClick={onDone}>
            Cancel
          </button>
        )}
        <span className="subtle">It runs there in a shared terminal; you'll be taken to it.</span>
      </div>
    </form>
  );
}
