import { lazy, Suspense, useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, machineProviders, preferredModel, providerModels, type Agent, type FleetMachine, type MachineProvider, type ProviderRef } from "../api";
import { CodeLine } from "../components/CodeLine";
import { FolderPicker } from "../components/FolderPicker";
import { Icon } from "../components/Icon";
import { ShareChoice } from "../components/Sharing";
import { StateDot } from "../components/ProviderState";
import { useAgents, useAuth, useClipboard, useMachines, useModels, useSessions, useStatus, useToast } from "../hooks";
import { providerLabel } from "../routes";
import { formatRelative } from "../format";
import { OllamaProvider } from "./Models";
import { StrataProvider } from "./Strata";
import { DockerGuide, DockerProvider } from "./Docker";
import { MachineOllama } from "./OllamaServer";
import { MachineLoad } from "./Hosts";
import { terminalState } from "./Terminals";

// Containers are rarely open; load them when visited.
const MachineContainers = lazy(() => import("./Containers").then((module) => ({ default: module.MachineContainers })));

// The model select's last choice: type a name instead.
const OTHER_MODEL = "__another__";

export const OS_LABELS: Record<string, string> = { windows: "Windows", linux: "Linux", darwin: "macOS", freebsd: "FreeBSD" };

/** A model server's name on its machine's page: "Ollama :11434", with its backend's name once it's one. */
export function providerName(provider: MachineProvider): string {
  return `${providerLabel(provider.kind)} :${provider.port}`;
}

export const machinePath = (id: string, section?: string) => `/machines/${encodeURIComponent(id)}${section ? `/${encodeURIComponent(section)}` : ""}`;

/**
 * One machine of the fleet, and everything it runs for the server: its model servers (each
 * with its models, settings and logs), the Ollama and Strata installed there, its containers,
 * its load, and starting agents on it. All of it is asked of the machine itself.
 */
export function MachinePage() {
  const { id = "", section = "" } = useParams();
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
  const provider = providers.find((item) => item.id === section);
  const ollamaInstalled = Boolean(machine.servers.ollama?.found);
  const runsContainers = Boolean(machine.containers.enabled) || providers.some((item) => item.container);

  return (
    <div className="page wide">
      <MachineHeader machine={machine} />
      <nav className="machine-nav" aria-label={`${machine.name} sections`}>
        <NavLink to={machinePath(machine.id)} end>
          Overview
        </NavLink>
        {providers.map((item) => (
          <NavLink key={item.id} to={machinePath(machine.id, item.id)}>
            <StateDot tone={machine.connected && item.detected ? "good" : "idle"} />
            {item.backend ?? providerName(item)}
            {item.container && <span className="nav-note">container</span>}
          </NavLink>
        ))}
        {ollamaInstalled && <NavLink to={machinePath(machine.id, "ollama")}>Ollama server</NavLink>}
        {runsContainers && <NavLink to={machinePath(machine.id, "containers")}>Containers</NavLink>}
        <NavLink to={machinePath(machine.id, "load")}>Load</NavLink>
      </nav>

      {section === "" && <MachineOverview machine={machine} providers={providers} />}
      {section === "load" && (
        <MachineLoad
          machine={machine.id}
          ollama={providers.filter((item) => item.kind === "ollama").map((item) => ({ target: refOf(machine, item), label: item.backend ?? providerName(item) }))}
        />
      )}
      {section === "ollama" && <MachineOllama machine={machine} />}
      {section === "containers" && (
        <Suspense fallback={<p className="subtle">Loading…</p>}>
          <MachineContainers machine={machine.id} />
        </Suspense>
      )}
      {provider && <ProviderView key={provider.id} machine={machine} provider={provider} />}
      {section && !provider && !["load", "ollama", "containers"].includes(section) && (
        <p className="empty">
          {machine.name} doesn't report a model server {section}. It may have stopped; see the <Link to={machinePath(machine.id)}>overview</Link>.
        </p>
      )}
    </div>
  );
}

const refOf = (machine: FleetMachine, provider: MachineProvider): ProviderRef => ({ machine: machine.id, provider: provider.id });

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
      {provider.kind === "ollama" && <OllamaProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
      {provider.kind === "strata" && <StrataProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
      {provider.kind === "docker" && <DockerProvider target={target} label={label} backend={provider.backend} manageable={manageable} />}
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
  const admin = !identity || identity.role === "admin";
  const adopt = useMutation({
    mutationFn: () => api.adoptProvider(machine.id, provider.kind),
    onSuccess: (result) => {
      notify(`Added the provider ${result.backend}. Agents can use it now.`);
      client.invalidateQueries();
    },
    onError: (error: Error) => notify(error.message),
  });
  if (!admin) return <p className="callout">Agents can use it once an admin adds it as a provider.</p>;
  return (
    <div className="callout row wrap">
      <span>Agents can't use it yet. Add it as a provider and every agent that speaks its API can.</span>
      <button type="button" className="primary small" disabled={adopt.isPending || !machine.connected} onClick={() => adopt.mutate()}>
        {adopt.isPending ? "Adding…" : "Add as a provider"}
      </button>
    </div>
  );
}

function MachineHeader({ machine }: { machine: FleetMachine }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const notify = useToast();
  const identity = useAuth().data?.identity;
  const agents = useAgents();
  const admin = !identity || identity.role === "admin";
  const [launching, setLaunching] = useState(false);
  const [shell, setShell] = useState(false);
  const installed = (agents.data ?? []).filter((agent) => machine.agents.includes(agent.id));
  const canLaunch = machine.connected && machine.launches && machine.you.launch && machine.roots.length > 0 && installed.length > 0 && machine.launcher !== false;
  const canShell = machine.connected && Boolean(machine.commands) && machine.you.launch && machine.roots.length > 0 && machine.launcher !== false;
  const forget = useMutation({
    mutationFn: () => api.forgetMachine(machine.id),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["machines"] });
      navigate("/machines");
    },
    onError: (error: Error) => notify(error.message),
  });
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
            {canLaunch && (
              <button type="button" className="primary" onClick={() => setLaunching((open) => !open)} aria-expanded={launching}>
                <Icon name="play" size={14} />
                Start an agent
              </button>
            )}
            {canShell && (
              <button type="button" onClick={() => setShell((open) => !open)} aria-expanded={shell}>
                <Icon name="terminal" size={14} />
                Open a terminal
              </button>
            )}
            {!machine.connected && (machine.you.owner || admin) && (
              <button type="button" className="icon-button danger" title="Forget this machine" aria-label={`Forget ${machine.name}`} disabled={forget.isPending} onClick={() => forget.mutate()}>
                <Icon name="trash" />
              </button>
            )}
          </div>
        </div>
      </header>
      {machine.update?.available && <UpdateNote machine={machine} update={machine.update} />}
      {launching && canLaunch && <LaunchForm machine={machine} agents={installed} onDone={() => setLaunching(false)} />}
      {shell && canShell && <ShellForm machine={machine} onDone={() => setShell(false)} />}
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

function MachineOverview({ machine, providers }: { machine: FleetMachine; providers: MachineProvider[] }) {
  const agents = useAgents();
  const installed = (agents.data ?? []).filter((agent) => machine.agents.includes(agent.id));
  const missing = [
    !machine.connected && "it isn't connected",
    machine.launcher === false && "the agent command isn't installed there (install the client, then agent login)",
    machine.roots.length === 0 && "it has no launch folders (agent-machine roots add <dir>)",
    installed.length === 0 && "none of the server's agents is installed there",
  ].filter((reason): reason is string => Boolean(reason));
  return (
    <div className="machine-overview">
      <MachineSessions machine={machine} />
      <section className="section" aria-labelledby="machine-servers">
        <div className="section-head">
          <h2 id="machine-servers">Model servers</h2>
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
                  {provider.container && " · container"}
                  {!provider.detected && " · not answering now"}
                </span>
                <span className="state-text">
                  <Link className="button small" to={machinePath(machine.id, provider.id)}>
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
              Start Ollama, Strata or Docker Model Runner there and agent-machine finds it within a minute. Or run{" "}
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

      <section className="section" aria-labelledby="machine-agents">
        <div className="section-head">
          <h2 id="machine-agents">Agents</h2>
        </div>
        <dl className="machine-facts">
          <div>
            <dt>Installed</dt>
            <dd className="chips">
              {installed.length ? installed.map((agent) => <span key={agent.id} className="pill">{agent.name}</span>) : <span className="subtle">None of the server's agents</span>}
            </dd>
          </div>
          <div>
            <dt>Launches</dt>
            <dd>
              {machine.launches ? (
                machine.roots.length ? (
                  <span className="chips">
                    {machine.roots.map((root) => (
                      <span key={root} className="pill mono">
                        {root}
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="subtle">
                    On, but no folders yet: <code>agent-machine roots add &lt;dir&gt;</code>
                  </span>
                )
              ) : (
                <span className="subtle">
                  Off. Run <code>agent-machine launches on</code> there to start agents from here.
                </span>
              )}
              {machine.launches && !machine.you.launch && <span className="subtle"> Only {machine.owner ?? "its owner"} can start agents on it.</span>}
              {machine.launches && machine.you.launch && missing.length > 0 && <span className="warn launch-missing">To start agents here: {missing.join("; ")}.</span>}
            </dd>
          </div>
          <div>
            <dt>Terminals</dt>
            <dd>
              {machine.commands ? (
                machine.you.launch ? (
                  <span>On: open one with <strong>Open a terminal</strong> above. It's a shell there that only you see.</span>
                ) : (
                  <span className="subtle">On, for {machine.owner ?? "its owner"} only.</span>
                )
              ) : (
                <span className="subtle">
                  Off. Run <code>agent-machine commands on</code> there to open a terminal on it from here.
                </span>
              )}
            </dd>
          </div>
        </dl>
        {machine.history.length > 0 && (
          <details className="manual">
            <summary>Recent launches</summary>
            <ul className="launch-history">
              {[...machine.history].reverse().map((launch) => (
                <li key={launch.launch}>
                  <span className={launch.error ? "bad" : "good"}>{launch.error ? "Failed" : "Started"}</span>
                  <strong>{agents.data?.find((agent) => agent.id === launch.agent)?.name ?? launch.agent}</strong>
                  <span className="mono subtle">{launch.project}</span>
                  <span className="subtle">
                    {launch.by && `by ${launch.by}, `}
                    {formatRelative(new Date(launch.at * 1000).toISOString())}
                  </span>
                  {launch.session_id ? <Link to={`/terminals/${launch.session_id}`}>Terminal</Link> : <span className="bad">{launch.error}</span>}
                </li>
              ))}
            </ul>
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
  const terminals = useQuery({ queryKey: ["terminals"], queryFn: api.terminals, retry: false, refetchInterval: (query) => (query.state.status === "error" ? false : 3000) });
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

type ServerAction = { kind: "ollama" | "strata"; action: "start" | "stop"; profile?: string };

/** Ollama and Strata installed on the machine, which its owner can start and stop from here (agent-machine servers on). */
function InstalledServers({ machine }: { machine: FleetMachine }) {
  const client = useQueryClient();
  const notify = useToast();
  const servers = machine.servers ?? {};
  const act = useMutation({
    mutationFn: ({ kind, action, profile }: ServerAction) => api.machineServer(machine.id, kind, action, profile),
    onSuccess: (_, { kind, action, profile }) => {
      const what = kind === "ollama" ? "Ollama" : `Strata ${profile}`;
      notify(action === "start" ? `${what} started on ${machine.name}.` : `${what} stopped on ${machine.name}.`);
      client.invalidateQueries({ queryKey: ["machines"] });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error: Error) => notify(error.message),
  });
  if (!servers.enabled) {
    return machine.you.owner ? (
      <p className="subtle">
        To start and stop the Ollama or Strata installed there, tune Ollama, and pull, load and delete models from here, run{" "}
        <code>agent-machine servers on</code> there.
      </p>
    ) : null;
  }
  const allowed = machine.connected && machine.you.launch;
  const busy = (kind: string, profile?: string) => act.isPending && act.variables?.kind === kind && act.variables?.profile === profile;
  const button = (request: ServerAction, label: string, primary = false) => (
    <button type="button" className={`small ${primary ? "primary" : ""}`} disabled={!allowed || act.isPending} onClick={() => act.mutate(request)}>
      {busy(request.kind, request.profile) ? (request.action === "start" ? "Starting…" : "Stopping…") : label}
    </button>
  );
  const ollama = servers.ollama;
  const strata = servers.strata;
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
              <Link className="button small" to={machinePath(machine.id, "ollama")}>
                Settings
              </Link>
            )}
          </span>
        </li>
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
            <span className="subtle">{strata.error ?? "No saved profiles: set one up with strata launch there"}</span>
            <span />
          </li>
        )}
        {strata?.profiles.map((profile) => {
          const stopped = profile.state === "stopped";
          return (
            <li key={profile.id}>
              <StateDot tone={stopped || profile.state === "unknown" ? "idle" : "good"} />
              <span className="provider-backend-name">
                <strong>Strata</strong>
                <span className="mono subtle">{profile.id}</span>
              </span>
              <span className="subtle" title={profile.error ?? undefined}>
                {stopped ? "Not running" : profile.state === "unknown" ? "State unknown" : `${profile.state[0].toUpperCase()}${profile.state.slice(1)}`}
                {profile.url && <span className="mono"> · {profile.url}</span>}
              </span>
              <span className="state-text">
                {stopped
                  ? button({ kind: "strata", action: "start", profile: profile.id }, "Start", true)
                  : profile.state !== "unknown" && button({ kind: "strata", action: "stop", profile: profile.id }, "Stop")}
              </span>
            </li>
          );
        })}
      </ul>
    </>
  );
}

/** A shell on the machine, in a terminal only you see: agent-machine commands on there. */
function ShellForm({ machine, onDone }: { machine: FleetMachine; onDone: () => void }) {
  const navigate = useNavigate();
  const notify = useToast();
  const client = useQueryClient();
  const [project, setProject] = useState(machine.roots[0] ?? "");
  const open = useMutation({
    mutationFn: () => api.openShell(machine.id, project),
    onSuccess: (result) => {
      client.invalidateQueries({ queryKey: ["machines"] });
      onDone();
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
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function LaunchForm({ machine, agents, onDone }: { machine: FleetMachine; agents: Agent[]; onDone: () => void }) {
  const navigate = useNavigate();
  const notify = useToast();
  const client = useQueryClient();
  const [agentId, setAgentId] = useState(agents[0]?.id ?? "");
  const [project, setProject] = useState(machine.roots[0] ?? "");
  const [model, setModel] = useState("");
  const [backend, setBackend] = useState("");
  const [trace, setTrace] = useState(false);
  const [share, setShare] = useState({ value: "private", control: false });
  const agent = agents.find((item) => item.id === agentId);
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
  // Fill in the provider's model when none is typed, or when the one there isn't this provider's.
  useEffect(() => {
    if (typing || agent?.intercept || !available.length || available.includes(model)) return;
    setModel(preferredModel(available));
  }, [agent, available, model, typing]);
  const describe = (name: string) => {
    const info = status.data?.backends[name];
    return `${name} (${providerLabel(info?.type, true)}${info?.machine_name ? ` on ${info.machine_name}` : ""})`;
  };
  const launch = useMutation({
    mutationFn: () => {
      const [visibility, team] = share.value.split(":");
      return api.launchOnMachine(machine.id, {
        agent: agentId, project, model: model.trim() || undefined, backend: backend || undefined, trace,
        visibility, team, access: share.control ? "control" : "view",
      });
    },
    onSuccess: (result) => {
      notify(`Started ${agent?.name ?? agentId} on ${machine.name}.`);
      client.invalidateQueries({ queryKey: ["machines"] });
      onDone();
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
                      ? `${available.length} ${available.length === 1 ? "model" : "models"} on ${effective}, aliases from models.yaml included.`
                      : `${effective} answers, but lists no models yet.`}
            </small>
          )}
        </label>
      </div>
      <ShareChoice value={share.value} control={share.control} onChange={(value, control) => setShare({ value, control })} />
      <label className="check">
        <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
        Capture full request and response payloads
      </label>
      <div className="row wrap">
        <button type="submit" className="primary" disabled={launch.isPending || !agentId || !project}>
          {launch.isPending ? "Starting…" : `Start on ${machine.name}`}
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
        <span className="subtle">It runs there in a shared terminal; you'll be taken to it.</span>
      </div>
    </form>
  );
}
