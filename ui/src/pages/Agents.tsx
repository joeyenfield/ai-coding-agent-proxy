import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, preferredModel, providerModels, PROTOCOL_LABELS, type Agent, type InstallEnvironment, type PreparedSession } from "../api";
import { CodeLine } from "../components/CodeLine";
import { useAgents, useAuth, useClipboard, useModels, useStatus } from "../hooks";
import { PartTag } from "../components/PartTag";
import { ShareChoice, shareArgs } from "../components/Sharing";
import { providerLabel, ROUTE_ORDER, ROUTES, type RouteKind } from "../routes";

const ROUTE_INTROS: Record<RouteKind, string> = {
  account: "Use the agent's own sign-in and plan. Nothing is translated: the proxy records every request on its way to the vendor.",
  ollama: "Run the agent against your own Ollama machines, Docker Model Runner or Strata, on your machines. For Ollama the proxy translates requests and applies the context size and sampling from models.yaml; Docker and Strata take them as they are.",
  hosted: "Point the agent's API endpoint at the proxy and pass requests through unchanged to a hosted API backend.",
};

/** Hosted backends don't list models through the proxy, so suggest the usual names instead. */
const HOSTED_MODELS = ["sonnet", "opus", "haiku"];

const routeOfAgent = (agent: Agent): RouteKind => agent.route ?? (agent.intercept ? "account" : "ollama");

export function AgentsPage() {
  const agents = useAgents();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const list = agents.data ?? [];
  const selected = list.find((agent) => agent.id === selectedId) ?? list[0];
  const groups = ROUTE_ORDER.map((route) => ({ route, agents: list.filter((agent) => routeOfAgent(agent) === route) })).filter(
    (group) => group.agents.length,
  );
  return (
    <div className="page wide">
      <header className="page-head">
        <PartTag />
        <h1>Start an agent</h1>
        <p className="lede">
          Every agent runs through this proxy so its requests are recorded. Choose how it reaches a model: your own account,
          a local model on Ollama, Docker or Strata, or a hosted API.
        </p>
      </header>
      {agents.isError && <p className="bad">{agents.error.message}</p>}
      <div className="split">
        <div className="split-main">
          {groups.map((group) => (
            <section key={group.route} className={`agent-group route-${group.route}`} aria-labelledby={`agents-${group.route}`}>
              <h2 id={`agents-${group.route}`}>
                <span className={`route-dot route-${group.route}`} aria-hidden="true" />
                {ROUTES[group.route].label}
              </h2>
              <p className="subtle">{ROUTE_INTROS[group.route]}</p>
              <ul className="agent-list" aria-label={ROUTES[group.route].label}>
                {group.agents.map((agent) => (
                  <li key={agent.id}>
                    <button
                      type="button"
                      className={`agent ${selected?.id === agent.id ? "selected" : ""}`}
                      aria-pressed={selected?.id === agent.id}
                      onClick={() => setSelectedId(agent.id)}
                    >
                      <span className="agent-name">
                        {agent.name}
                        {agent.recommended_for.includes("qwen") && <span className="pill accent">Good with Qwen</span>}
                      </span>
                      <span className="subtle">{agent.description}</span>
                      <span className="agent-meta">
                        <span className={agent.installed ? "good" : "subtle"}>{agent.installed ? "Installed" : "Not installed"}</span>
                        <span className="subtle">{agent.intercept ? "Full traffic capture" : PROTOCOL_LABELS[agent.protocol]}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
        <div className="split-side">{selected && <LaunchPanel key={selected.id} agent={selected} />}</div>
      </div>
    </div>
  );
}

function LaunchPanel({ agent }: { agent: Agent }) {
  if (agent.intercept) return <AccountLaunchPanel agent={agent} />;
  return <ProxyLaunchPanel agent={agent} />;
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

/** Agents signed in to their own service: no backend or model to pick, traffic is captured in transit. */
function AccountLaunchPanel({ agent }: { agent: Agent }) {
  const status = useStatus();
  const copy = useClipboard();
  const client = useQueryClient();
  const [trace, setTrace] = useState(true);
  const [control, setControl] = useState(false);
  const [share, setShare] = useState({ value: "private", control: false });
  const identity = useAuth().data?.identity;
  const [shell, setShell] = useState<"powershell" | "bash">(navigator.userAgent.includes("Windows") ? "powershell" : "bash");
  const [prepared, setPrepared] = useState<PreparedSession | null>(null);
  const intercept = status.data?.intercept;
  const prepare = useMutation({
    mutationFn: () => api.prepareAgent(agent.id, { model: "", backend: "", trace }),
    onSuccess: (result) => {
      setPrepared(result);
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  const launcher = ["agent", agent.id, ".", ...(trace ? ["--trace"] : []), ...(control ? ["--control"] : []), ...shareArgs(identity, share.value, share.control)].join(" ");
  return (
    <section className="panel" aria-labelledby="launch-title">
      <h2 id="launch-title">Start {agent.name}</h2>
      <p>
        <span className="route-badge route-account">{ROUTES.account.label}</span>
      </p>
      <InstallCommand agent={agent} />
      <p>
        The agent keeps its own sign-in and talks to its own service. The launcher sends all of its HTTPS to the
        agent-proxy on its machine, which carries it here to be recorded. Every request it makes shows up on the{" "}
        <Link to="/traffic">Traffic</Link> page, and model calls also stream on the <Link to="/live">Live</Link> page.
      </p>
      {intercept && !intercept.running ? (
        <p className="bad">HTTPS capture is off on this server: {intercept.error}</p>
      ) : (
        intercept && (
          <p className="subtle">
            The agent-proxy's HTTPS proxy is <span className="mono">{intercept.url}</span> on each agent machine. This
            server's certificate authority is trusted only by the processes the launcher starts.
          </p>
        )
      )}
      <label className="check">
        <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
        Save full request and response bodies (credentials are redacted)
      </label>
      <ControlCheck checked={control} onChange={setControl} />
      <ShareChoice value={share.value} control={share.control} onChange={(value, control) => setShare({ value, control })} />
      <h3>Run it from your project folder</h3>
      <CodeLine text={launcher} onCopy={() => copy(launcher, "Command copied")} />
      <p className="subtle">Arguments after <code>--</code> go to the agent, for example <code>{launcher} -- --model sonnet</code>.</p>
      {agent.notes && <p className="subtle">{agent.notes}</p>}
      <details className="manual" onToggle={() => prepare.reset()}>
        <summary>Set it up by hand instead</summary>
        <p className="subtle">
          Opens a session now and shows the environment for {agent.executable}, on a machine whose agent-proxy is
          running. Started this way, its terminal isn't shared with Running agents.
        </p>
        <button type="button" disabled={prepare.isPending || !intercept?.running} onClick={() => prepare.mutate()}>
          Open session and show environment
        </button>
        {prepare.isError && <p className="bad">{prepare.error.message}</p>}
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
          </>
        )}
      </details>
      {agent.homepage && (
        <p>
          <a href={agent.homepage} target="_blank" rel="noreferrer">
            {agent.name} documentation
          </a>
        </p>
      )}
    </section>
  );
}

function ProxyLaunchPanel({ agent }: { agent: Agent }) {
  const status = useStatus();
  const models = useModels();
  const copy = useClipboard();
  const client = useQueryClient();
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
  const [shell, setShell] = useState<"powershell" | "bash">(navigator.userAgent.includes("Windows") ? "powershell" : "bash");
  const [prepared, setPrepared] = useState<PreparedSession | null>(null);

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

  const prepare = useMutation({
    mutationFn: () => api.prepareAgent(agent.id, { model, backend, trace }),
    onSuccess: (result) => {
      setPrepared(result);
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  // The launcher picks preferredBackend itself, so only name a different one.
  const launcher = ["agent", agent.id, ".", "--model", model || "<model>", ...(backend && backend !== preferredBackend ? ["--backend", backend] : []), ...(trace ? ["--trace"] : []), ...(control ? ["--control"] : []), ...shareArgs(identity, share.value, share.control)].join(" ");
  const backendState = route === "ollama" ? models.data?.backends[backend] : undefined;

  return (
    <section className="panel" aria-labelledby="launch-title">
      <h2 id="launch-title">Start {agent.name}</h2>
      <p>
        <span className={`route-badge route-${route}`}>{ROUTES[route].label}</span>{" "}
        <span className="subtle">{ROUTES[route].description}</span>
      </p>
      <InstallCommand agent={agent} />
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
      <label className="check">
        <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
        Capture full request and response payloads
      </label>
      <ControlCheck checked={control} onChange={setControl} />
      <ShareChoice value={share.value} control={share.control} onChange={(value, control) => setShare({ value, control })} />

      <h3>Run it from your project folder</h3>
      <CodeLine text={launcher} onCopy={() => copy(launcher, "Command copied")} />
      <p className="subtle">
        The launcher opens a session through the agent-proxy on its machine, points the agent at it and closes the
        session when the agent exits. The agent-proxy carries every request here.
      </p>
      {agent.notes && <p className="subtle">{agent.notes}</p>}

      <details className="manual" onToggle={() => prepare.reset()}>
        <summary>Set it up by hand instead</summary>
        <p className="subtle">
          Opens a session now and shows the environment for {agent.executable}, on a machine whose agent-proxy is
          running. Started this way, its terminal isn't shared with Running agents.
        </p>
        <button type="button" disabled={!model || prepare.isPending} onClick={() => prepare.mutate()}>
          Open session and show environment
        </button>
        {prepare.isError && <p className="bad">{prepare.error.message}</p>}
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
          </>
        )}
      </details>
      {agent.homepage && (
        <p>
          <a href={agent.homepage} target="_blank" rel="noreferrer">
            {agent.name} documentation
          </a>
        </p>
      )}
    </section>
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
  const saved = localStorage.getItem(ENVIRONMENT_KEY);
  if (saved && ENVIRONMENTS.some((item) => item.id === saved)) return saved as InstallEnvironment;
  const platform = navigator.userAgent.toLowerCase();
  if (platform.includes("windows")) return "windows";
  if (platform.includes("mac os") || platform.includes("macintosh")) return "macos";
  return "linux";
}

/** How to install the agent where it'll run: the command for the environment picked, always shown. */
function InstallCommand({ agent }: { agent: Agent }) {
  const copy = useClipboard();
  const [environment, setEnvironment] = useState<InstallEnvironment>(guessEnvironment);
  const pick = (value: InstallEnvironment) => {
    setEnvironment(value);
    localStorage.setItem(ENVIRONMENT_KEY, value);
  };
  const command = agent.installs?.[environment] ?? agent.install;
  return (
    <div className="install-command">
      <div className="install-command-head">
        <h3>Install {agent.name}</h3>
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
        {agent.installed ? (
          <>
            Installed on the server's computer (<code>{agent.executable}</code> is on its PATH).{" "}
          </>
        ) : (
          <>
            <code>{agent.executable}</code> isn't on the server's computer. Install it on the computer that runs the agent.{" "}
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
