import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, preferredModel, providerModels, type Agent, type FleetMachine, type InstallEnvironment, type PreparedSession } from "../api";
import { CodeLine } from "../components/CodeLine";
import { McpChoice, McpUrls, mcpArgs } from "../components/McpChoice";
import { remember, remembered, useAgents, useAuth, useClipboard, useMachines, useModels, useStatus } from "../hooks";
import { PartTag } from "../components/PartTag";
import { ShareChoice, shareArgs } from "../components/Sharing";
import { providerLabel, ROUTE_ORDER, ROUTES, type RouteKind } from "../routes";
import { LaunchForm } from "./Machine";

/** One line on each route, under the agent's name. */
const ROUTE_LINES: Record<RouteKind, string> = {
  account: "Uses its own sign-in and plan. The server records everything it sends.",
  ollama: "Runs on a model from your machines: Ollama, llama.cpp, Strata or Docker Model Runner.",
  hosted: "Uses a hosted API, such as Anthropic, through the server.",
};

/** Hosted backends don't list models through the proxy, so suggest the usual names instead. */
const HOSTED_MODELS = ["sonnet", "opus", "haiku"];

/** The last place agents were started from (a machine's id, or "" for the command), for the next visit. */
const START_ON_KEY = "ai-proxy-start-on";

const routeOfAgent = (agent: Agent): RouteKind => agent.route ?? (agent.intercept ? "account" : "ollama");


/** Why no agent can be started on the machine now, or null when it can take launches. */
function machineBlocker(machine: FleetMachine): string | null {
  if (!machine.you.launch) return "you can't start agents there";
  if (!machine.connected) return "offline";
  if (!machine.launches) return "launches are off";
  if (machine.launcher === false) return "the agent command isn't installed";
  if (!machine.roots.length) return "no launch folders";
  return null;
}

/**
 * Start an agent: where it runs (one of your machines, or a command to run yourself), then which
 * agent, then only what that choice needs. The choices are in the address (?on=&agent=), so a link
 * opens the same view.
 */
export function AgentsPage() {
  const agents = useAgents();
  const machines = useMachines();
  const [params, setParams] = useSearchParams();
  const list = agents.data ?? [];
  // Machines you may start agents on; the others never could, so they aren't offered.
  const fleet = (machines.data?.machines ?? []).filter((machine) => machine.you.launch);
  const asked = params.get("on") ?? remembered(START_ON_KEY) ?? "";
  const machine = fleet.find((item) => item.id === asked);
  // Until the machines arrive, keep the one asked for, so choosing an agent meanwhile doesn't drop it.
  const on = machine?.id ?? (machines.isPending ? asked : "");
  const agent = list.find((item) => item.id === params.get("agent"));

  const choose = (next: { on?: string; agent?: string }) => {
    const values = { on, agent: agent?.id ?? params.get("agent") ?? "", ...next };
    if (next.on !== undefined) remember(START_ON_KEY, next.on);
    const search = new URLSearchParams();
    if (values.on) search.set("on", values.on);
    if (values.agent) search.set("agent", values.agent);
    setParams(search, { replace: true });
  };

  const installedHere = (item: Agent) => Boolean(machine?.agents.includes(item.id));
  const groups = machine
    ? [
        { label: `Installed on ${machine.name}`, agents: list.filter(installedHere) },
        { label: `Not installed on ${machine.name}`, agents: list.filter((item) => !installedHere(item)) },
      ]
    : ROUTE_ORDER.map((route) => ({ label: ROUTES[route].label, agents: list.filter((item) => routeOfAgent(item) === route) }));

  return (
    <div className="page start-page">
      <header className="page-head">
        <PartTag />
        <h1>Start an agent</h1>
        <p className="lede">
          Pick where it runs and which coding agent. Everything it sends goes through this server, so you can watch it and
          see every request it makes.
        </p>
      </header>
      {agents.isError && <p className="bad">{agents.error.message}</p>}
      {machines.isSuccess && !fleet.length && (
        <div className="callout row start-no-machines">
          <span>
            <strong>No machines yet.</strong> Add a computer as a machine to start agents on it from here, in one click, and
            watch them from any device. Until then, run them yourself with a command.
          </span>
          <Link to="/machines#add" className="button primary">
            Add a machine
          </Link>
        </div>
      )}
      <section className="panel start-picker" aria-label="What to start">
        <div className="form-grid two">
          <label className="field">
            <span className="start-step">1. Where should it run?</span>
            <select value={on} onChange={(event) => choose({ on: event.target.value })}>
              <option value="">Give me the command</option>
              {fleet.length > 0 && (
                <optgroup label="On one of your machines">
                  {fleet.map((item) => {
                    const reason = machineBlocker(item);
                    return (
                      <option key={item.id} value={item.id}>
                        {item.name}
                        {reason ? ` (${reason})` : ""}
                      </option>
                    );
                  })}
                </optgroup>
              )}
            </select>
            <small>
              {machine
                ? machineBlocker(machine)
                  ? `${machine.name} can't start agents now: ${machineBlocker(machine)}.`
                  : `It starts on ${machine.name} in a terminal you can watch and type into from any device.`
                : machines.isPending
                  ? "Looking for your machines…"
                  : "You run it yourself in a terminal, on any computer with the agent command."}
            </small>
          </label>
          <label className="field">
            <span className="start-step">2. Which agent?</span>
            <select value={agent?.id ?? ""} onChange={(event) => choose({ agent: event.target.value })} disabled={!list.length}>
              <option value="">{agents.isPending ? "Loading agents…" : "Choose an agent…"}</option>
              {groups
                .filter((group) => group.agents.length)
                .map((group) => (
                  <optgroup key={group.label} label={group.label}>
                    {group.agents.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </optgroup>
                ))}
            </select>
            <small>
              {machine
                ? `${list.filter(installedHere).length} of ${list.length} installed on ${machine.name}. Any other shows how to install it.`
                : "Agents signed in to your own plan, or running on your machines' models or a hosted API."}
            </small>
          </label>
        </div>
      </section>
      {agent ? (
        <AgentStart key={`${on}:${agent.id}`} agent={agent} machine={machine} />
      ) : (
        <p className="subtle start-hint">Choose an agent to see how to start it.</p>
      )}
    </div>
  );
}

/** The chosen agent: what it is, then how to start it where it was asked to run. */
function AgentStart({ agent, machine }: { agent: Agent; machine?: FleetMachine }) {
  const agents = useAgents();
  const route = routeOfAgent(agent);
  return (
    <section className="panel agent-start" aria-labelledby="agent-start-title">
      <div className="agent-start-head">
        <h2 id="agent-start-title">{agent.name}</h2>
        <span className={`route-badge route-${route}`}>{ROUTES[route].label}</span>
        {agent.recommended_for.includes("qwen") && <span className="pill accent">Good with Qwen</span>}
      </div>
      <p>
        {agent.description ? `${agent.description} ` : ""}
        <span className="subtle">{ROUTE_LINES[route]}</span>{" "}
        {agent.homepage && (
          <a href={agent.homepage} target="_blank" rel="noreferrer">
            Its documentation
          </a>
        )}
      </p>
      {machine ? (
        <MachineStart agent={agent} machine={machine} agents={agents.data ?? []} />
      ) : (
        <>
          <h3>3. Install {agent.name}</h3>
          <InstallCommand agent={agent} />
          <h3>4. Start it from your project folder</h3>
          <p className="subtle">
            It needs the <code>agent</code> command, signed in to this server: get it from <Link to="/get">Downloads</Link>,
            then run <code>agent login</code> once.
          </p>
          {agent.intercept ? <AccountCommand agent={agent} /> : <ProxyCommand agent={agent} />}
        </>
      )}
    </section>
  );
}

/** Starting it on a machine: why it can't, how to install it there, or the launch form. */
function MachineStart({ agent, machine, agents }: { agent: Agent; machine: FleetMachine; agents: Agent[] }) {
  const blocker = machineBlocker(machine);
  if (blocker) {
    return (
      <div className="callout">
        <span>
          <strong>{machine.name} can't start agents now:</strong> {blocker}.
        </span>
        <span>
          <Link to={`/machines/${encodeURIComponent(machine.id)}`}>Open {machine.name}</Link> to see why, or choose{" "}
          <em>Give me the command</em> above to run it yourself.
        </span>
      </div>
    );
  }
  if (!machine.agents.includes(agent.id)) {
    return (
      <>
        <h3>3. Install {agent.name} on {machine.name}</h3>
        <InstallCommand agent={agent} initial={environmentOf(machine)} where={machine.name} />
        <p className="subtle">
          {machine.name} tells the server what's installed every minute: once {agent.executable} is on its PATH, you can start it
          here.
        </p>
      </>
    );
  }
  return (
    <>
      <h3>3. Start it on {machine.name}</h3>
      <LaunchForm machine={machine} agents={agents.filter((item) => machine.agents.includes(item.id))} initialAgent={agent.id} fixedAgent />
    </>
  );
}

/** --control: share the agent's terminal with this server, to watch and type into from Running agents. */
function ControlCheck({ checked, onChange }: { checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span>
        Watch and type into it from <Link to="/terminals">Running agents</Link>, on any device (<code>--control</code>)
      </span>
    </label>
  );
}

/** Setting up an agent's environment without the launcher: a session now, and the variables to set. */
function ManualSetup({ agent, disabled, prepare }: { agent: Agent; disabled: boolean; prepare: () => Promise<PreparedSession> }) {
  const copy = useClipboard();
  const client = useQueryClient();
  const [shell, setShell] = useState<"powershell" | "bash">(navigator.userAgent.includes("Windows") ? "powershell" : "bash");
  const [prepared, setPrepared] = useState<PreparedSession | null>(null);
  const open = useMutation({
    mutationFn: prepare,
    onSuccess: (result) => {
      setPrepared(result);
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  return (
    <details className="manual" onToggle={() => open.reset()}>
      <summary>Set it up by hand instead</summary>
      <p className="subtle">
        Opens a session now and shows the environment for {agent.executable}, on a machine whose agent-proxy is running.
        Started this way, its terminal isn't shared with Running agents.
      </p>
      <button type="button" disabled={disabled || open.isPending} onClick={() => open.mutate()}>
        Open session and show environment
      </button>
      {open.isError && <p className="bad">{open.error.message}</p>}
      {prepared && (
        <>
          <div className="tabs" role="tablist" aria-label="Shell">
            {(["powershell", "bash"] as const).map((name) => (
              <button key={name} type="button" role="tab" aria-selected={shell === name} onClick={() => setShell(name)}>
                {name === "powershell" ? "PowerShell" : "bash / zsh"}
              </button>
            ))}
          </div>
          <div className="code-block">
            <pre className="code">{prepared.shell[shell]}</pre>
            <button type="button" onClick={() => copy(prepared.shell[shell], "Environment copied")}>
              Copy
            </button>
          </div>
          <McpUrls servers={prepared.mcp} />
        </>
      )}
    </details>
  );
}

/** The command for an agent signed in to its own service: no provider or model to pick, traffic captured in transit. */
function AccountCommand({ agent }: { agent: Agent }) {
  const status = useStatus();
  const copy = useClipboard();
  const [trace, setTrace] = useState(true);
  const [control, setControl] = useState(false);
  const [share, setShare] = useState({ value: "private", control: false });
  const identity = useAuth().data?.identity;
  const [mcp, setMcp] = useState<string[]>([]);
  const intercept = status.data?.intercept;
  const launcher = ["agent", agent.id, ".", ...(trace ? ["--trace"] : []), ...(control ? ["--control"] : []), ...mcpArgs(mcp), ...shareArgs(identity, share.value, share.control)].join(" ");
  return (
    <>
      {intercept && !intercept.running && <p className="bad">HTTPS capture is off on this server: {intercept.error}</p>}
      <CodeLine text={launcher} onCopy={() => copy(launcher, "Command copied")} />
      <p className="subtle">
        Arguments after <code>--</code> go to the agent, for example <code>{launcher} -- --model sonnet</code>. Its requests show
        up on <Link to="/traffic">Traffic</Link>.
      </p>
      <details className="start-options">
        <summary>Options: payloads, watching, MCP servers, sharing</summary>
        <label className="check">
          <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
          Save full request and response bodies (credentials are redacted)
        </label>
        <ControlCheck checked={control} onChange={setControl} />
        <McpChoice agent={agent} chosen={mcp} onChange={setMcp} />
        <ShareChoice value={share.value} control={share.control} onChange={(value, control) => setShare({ value, control })} />
      </details>
      {agent.notes && <p className="subtle">{agent.notes}</p>}
      <ManualSetup agent={agent} disabled={!intercept?.running} prepare={() => api.prepareAgent(agent.id, { model: "", backend: "", trace, mcp })} />
    </>
  );
}

/** The command for an agent the server points at a provider: pick the provider and model. */
function ProxyCommand({ agent }: { agent: Agent }) {
  const status = useStatus();
  const models = useModels();
  const copy = useClipboard();
  const route = routeOfAgent(agent);
  // The proxy lists the backends this agent's protocol can use: Ollama, and Docker or Strata for local-model agents.
  const backends = (agent.backends ?? []).filter((name) => status.data?.backends[name]);
  const offersPassThrough = backends.some((name) => status.data?.backends[name]?.type !== "ollama");
  // The one the server picks when a launch names none.
  const preferredBackend = backends.includes(agent.default_backend ?? "") ? agent.default_backend! : backends[0] ?? "";
  const [backend, setBackend] = useState("");
  const [model, setModel] = useState("");
  const [trace, setTrace] = useState(false);
  const [control, setControl] = useState(false);
  const [share, setShare] = useState({ value: "private", control: false });
  const identity = useAuth().data?.identity;
  const [mcp, setMcp] = useState<string[]>([]);

  useEffect(() => {
    if (!backend && preferredBackend) setBackend(preferredBackend);
  }, [backend, preferredBackend]);
  const available = useMemo(() => {
    if (route === "hosted") return HOSTED_MODELS;
    return providerModels(models.data?.backends[backend], models.data?.aliases, status.data?.backends[backend]?.type);
  }, [models.data, status.data, backend, route]);
  useEffect(() => {
    if (route === "hosted" ? model : !available.length || available.includes(model)) return;
    setModel(route === "hosted" ? HOSTED_MODELS[0] : preferredModel(available));
  }, [available, model, route]);

  // The launcher picks preferredBackend itself, so only name a different one.
  const launcher = ["agent", agent.id, ".", "--model", model || "<model>", ...(backend && backend !== preferredBackend ? ["--backend", backend] : []), ...(trace ? ["--trace"] : []), ...(control ? ["--control"] : []), ...mcpArgs(mcp), ...shareArgs(identity, share.value, share.control)].join(" ");
  const backendState = route === "ollama" ? models.data?.backends[backend] : undefined;

  return (
    <>
      {!backends.length ? (
        <p className="bad">
          No {route === "ollama" ? "local-model provider (Ollama, Docker or Strata)" : `${providerLabel(agent.backend_type ?? undefined)} provider`} that this
          agent can use is set up.{" "}
          {route === "ollama" ? (
            <>
              Local providers come from your machines: add one on <Link to="/machines">Machines</Link>.
            </>
          ) : (
            <>
              Add a hosted API on <Link to="/settings">Server settings</Link>.
            </>
          )}
        </p>
      ) : (
        <div className="form-grid two">
          <label className="field">
            {route !== "ollama" ? "Hosted API" : offersPassThrough ? "Local provider" : "Ollama provider"}
            <select value={backend} onChange={(event) => setBackend(event.target.value)}>
              {backends.map((name) => (
                <option key={name} value={name}>
                  {name} ({providerLabel(status.data?.backends[name]?.type, true)}
                  {status.data?.backends[name]?.machine_name ? ` on ${status.data.backends[name].machine_name}` : `, ${hostOf(status.data?.backends[name]?.url ?? "")}`})
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Model
            {route === "hosted" ? (
              <>
                <input list="hosted-models" value={model} onChange={(event) => setModel(event.target.value)} />
                <datalist id="hosted-models">
                  {HOSTED_MODELS.map((name) => (
                    <option key={name} value={name} />
                  ))}
                </datalist>
              </>
            ) : (
              <select value={model} onChange={(event) => setModel(event.target.value)} disabled={!available.length}>
                {available.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            )}
          </label>
        </div>
      )}
      {backendState && !backendState.online && <p className="bad">{backend} is offline: {backendState.error}</p>}
      <CodeLine text={launcher} onCopy={() => copy(launcher, "Command copied")} />
      <details className="start-options">
        <summary>Options: payloads, watching, MCP servers, sharing</summary>
        <label className="check">
          <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
          Capture full request and response payloads
        </label>
        <ControlCheck checked={control} onChange={setControl} />
        <McpChoice agent={agent} chosen={mcp} onChange={setMcp} />
        <ShareChoice value={share.value} control={share.control} onChange={(value, control) => setShare({ value, control })} />
      </details>
      {agent.notes && <p className="subtle">{agent.notes}</p>}
      <ManualSetup agent={agent} disabled={!model} prepare={() => api.prepareAgent(agent.id, { model, backend, trace, mcp })} />
    </>
  );
}

function hostOf(url: string) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

const ENVIRONMENTS: { id: InstallEnvironment; label: string }[] = [
  { id: "linux", label: "Linux" },
  { id: "macos", label: "macOS" },
  { id: "windows", label: "Windows" },
  { id: "wsl", label: "WSL" },
];
const ENVIRONMENT_KEY = "ai-proxy-install-environment";

/** The browser's own system, until the user picks another. */
function guessEnvironment(): InstallEnvironment {
  const saved = remembered(ENVIRONMENT_KEY);
  if (saved && ENVIRONMENTS.some((item) => item.id === saved)) return saved as InstallEnvironment;
  const platform = navigator.userAgent.toLowerCase();
  if (platform.includes("windows")) return "windows";
  if (platform.includes("mac os") || platform.includes("macintosh")) return "macos";
  return "linux";
}

/** The system a machine runs, for its install command. */
function environmentOf(machine: FleetMachine): InstallEnvironment {
  if (machine.os === "windows") return "windows";
  if (machine.os === "darwin" || machine.os === "macos") return "macos";
  return "linux";
}

/**
 * How to install the agent where it'll run: the command for the environment picked.
 * initial and where: the machine it's for, whose system is picked first.
 */
function InstallCommand({ agent, initial, where }: { agent: Agent; initial?: InstallEnvironment; where?: string }) {
  const copy = useClipboard();
  const [environment, setEnvironment] = useState<InstallEnvironment>(() => initial ?? guessEnvironment());
  const pick = (value: InstallEnvironment) => {
    setEnvironment(value);
    if (!initial) remember(ENVIRONMENT_KEY, value);
  };
  const command = agent.installs?.[environment] ?? agent.install;
  return (
    <div className="install-command">
      <div className="install-command-head">
        <span className="subtle">{where ? `Run this on ${where}` : "Run this on the computer the agent runs on"}</span>
        <div className="env-switch" role="radiogroup" aria-label="Where it runs">
          {ENVIRONMENTS.map((item) => (
            <button key={item.id} type="button" role="radio" aria-checked={environment === item.id} onClick={() => pick(item.id)}>
              {item.label}
            </button>
          ))}
        </div>
      </div>
      {command ? (
        <CodeLine text={command} onCopy={() => copy(command, "Install command copied")} />
      ) : (
        <p className="subtle">The server has no install command for it.</p>
      )}
      <p className="subtle">
        {!where && (
          <>
            Skip this if <code>{agent.executable}</code> is installed already.{" "}
          </>
        )}
        {environment === "wsl" && command?.startsWith("npm ") && (
          <>
            In WSL, use a Linux Node.js (<code>sudo apt install nodejs npm</code>, or nvm): Windows' npm installs the Windows build, which
            agents started in WSL can't run.{" "}
          </>
        )}
        {environment === "windows" && command?.includes("| bash") && <>Run it in Git Bash. </>}
        {agent.homepage && (
          <a href={agent.homepage} target="_blank" rel="noreferrer">
            Other ways to install it
          </a>
        )}
      </p>
    </div>
  );
}
