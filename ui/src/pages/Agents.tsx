import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Agent, type PreparedSession } from "../api";
import { useAgents, useClipboard, useModels, useStatus } from "../hooks";
import { ROUTE_ORDER, ROUTES, type RouteKind } from "../routes";

const ROUTE_INTROS: Record<RouteKind, string> = {
  account: "Use the agent's own sign-in and plan. Nothing is translated: the proxy records every request on its way to the vendor.",
  ollama: "Run the agent against your own Ollama machines. The proxy translates requests and applies the context size and sampling from models.yaml.",
  hosted: "Point the agent's API endpoint at the proxy and pass requests through unchanged to a hosted API backend.",
};

/** Hosted backends don't list models through the proxy, so suggest the usual names instead. */
const HOSTED_MODELS = ["sonnet", "opus", "haiku"];

const routeOfAgent = (agent: Agent): RouteKind => agent.route ?? (agent.intercept ? "account" : "ollama");

const PROTOCOL_LABELS: Record<Agent["protocol"], string> = {
  "openai-chat": "OpenAI Chat Completions",
  "openai-responses": "OpenAI Responses",
  anthropic: "Anthropic Messages",
  ollama: "Native Ollama",
};

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
        <h1>Start an agent</h1>
        <p className="lede">
          Every agent runs through this proxy so its requests are recorded. Choose how it reaches a model: your own account,
          your Ollama machines, or a hosted API.
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

/** Agents signed in to their own service: no backend or model to pick, traffic is captured in transit. */
function AccountLaunchPanel({ agent }: { agent: Agent }) {
  const status = useStatus();
  const copy = useClipboard();
  const client = useQueryClient();
  const [trace, setTrace] = useState(true);
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
  const launcher = ["agent", agent.id, ".", ...(trace ? ["--trace"] : [])].join(" ");
  return (
    <section className="panel" aria-labelledby="launch-title">
      <h2 id="launch-title">Start {agent.name}</h2>
      <p>
        <span className="route-badge route-account">{ROUTES.account.label}</span>
      </p>
      {!agent.installed && agent.install && (
        <div className="callout">
          <p>
            <code>{agent.executable}</code> isn't on this machine's PATH. Install it first:
          </p>
          <CodeLine text={agent.install} onCopy={() => copy(agent.install!, "Install command copied")} />
        </div>
      )}
      <p>
        The agent keeps its own sign-in and talks to its own service. The launcher sends all of its HTTPS through the
        proxy's intercept listener, so every request it makes shows up on the <Link to="/traffic">Traffic</Link> page,
        and model calls also stream on the <Link to="/live">Live</Link> page.
      </p>
      {intercept && !intercept.running ? (
        <p className="bad">The intercept listener isn't running: {intercept.error}</p>
      ) : (
        intercept && (
          <p className="subtle">
            Intercept listener <span className="mono">{intercept.url}</span>. Its certificate authority is trusted only by
            the processes the launcher starts.
          </p>
        )
      )}
      <label className="check">
        <input type="checkbox" checked={trace} onChange={(event) => setTrace(event.target.checked)} />
        Save full request and response bodies (credentials are redacted)
      </label>
      <h3>Run it from your project folder</h3>
      <CodeLine text={launcher} onCopy={() => copy(launcher, "Command copied")} />
      <p className="subtle">Arguments after <code>--</code> go to the agent, for example <code>{launcher} -- --model sonnet</code>.</p>
      {agent.notes && <p className="subtle">{agent.notes}</p>}
      <details className="manual" onToggle={() => prepare.reset()}>
        <summary>Set it up by hand instead</summary>
        <p className="subtle">Opens a session now and shows the environment for {agent.executable}.</p>
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
  const backendType = agent.backend_type ?? "ollama";
  // Only offer backends this agent's protocol can use.
  const backends = Object.entries(status.data?.backends ?? {})
    .filter(([, value]) => value.type === backendType)
    .map(([name]) => name);
  const preferredBackend = backends.includes(status.data?.default_backend ?? "") ? status.data!.default_backend : backends[0] ?? "";
  const [backend, setBackend] = useState("");
  const [model, setModel] = useState("");
  const [trace, setTrace] = useState(false);
  const [shell, setShell] = useState<"powershell" | "bash">(navigator.userAgent.includes("Windows") ? "powershell" : "bash");
  const [prepared, setPrepared] = useState<PreparedSession | null>(null);

  useEffect(() => {
    if (!backend && preferredBackend) setBackend(preferredBackend);
  }, [backend, preferredBackend]);
  const available = useMemo(() => {
    if (route === "hosted") return HOSTED_MODELS;
    const names = (models.data?.backends[backend]?.models ?? []).map((item) => item.name);
    return [...Object.keys(models.data?.aliases ?? {}), ...names];
  }, [models.data, backend, route]);
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
  const launcher = ["agent", agent.id, ".", "--model", model || "<model>", ...(backend && backend !== preferredBackend ? ["--backend", backend] : []), ...(trace ? ["--trace"] : [])].join(" ");
  const backendState = route === "ollama" ? models.data?.backends[backend] : undefined;

  return (
    <section className="panel" aria-labelledby="launch-title">
      <h2 id="launch-title">Start {agent.name}</h2>
      <p>
        <span className={`route-badge route-${route}`}>{ROUTES[route].label}</span>{" "}
        <span className="subtle">{ROUTES[route].description}</span>
      </p>
      {!agent.installed && agent.install && (
        <div className="callout">
          <p>
            <code>{agent.executable}</code> isn't on this machine's PATH. Install it first:
          </p>
          <CodeLine text={agent.install} onCopy={() => copy(agent.install!, "Install command copied")} />
        </div>
      )}
      {!backends.length ? (
        <p className="bad">
          No {backendType === "ollama" ? "Ollama" : backendType} backend is configured. Add one on the <Link to="/settings">Settings</Link> page.
        </p>
      ) : (
        <div className="form-grid two">
          <label className="field">
            {route === "ollama" ? "Ollama machine" : "Hosted backend"}
            <select value={backend} onChange={(event) => setBackend(event.target.value)}>
              {backends.map((name) => (
                <option key={name} value={name}>
                  {name} ({hostOf(status.data?.backends[name]?.url ?? "")})
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

      <h3>Run it from your project folder</h3>
      <CodeLine text={launcher} onCopy={() => copy(launcher, "Command copied")} />
      <p className="subtle">
        The launcher opens a session, sets the agent's environment and closes the session when the agent exits.
      </p>
      {agent.notes && <p className="subtle">{agent.notes}</p>}

      <details className="manual" onToggle={() => prepare.reset()}>
        <summary>Set it up by hand instead</summary>
        <p className="subtle">Opens a session now and shows the environment for {agent.executable}.</p>
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

function CodeLine({ text, onCopy }: { text: string; onCopy: () => void }) {
  return (
    <div className="code-block">
      <pre className="code">{text}</pre>
      <button type="button" onClick={onCopy}>
        Copy
      </button>
    </div>
  );
}

/** Prefer the newest Qwen 3.x release, coder variants first, then any Qwen, then anything. */
function preferredModel(names: string[]): string {
  const version = (name: string) => Number(/^qwen3\.(\d+)/.exec(name)?.[1] ?? -1);
  const coder = (name: string) => (/cod(er|ing)/.test(name) ? 1 : 0);
  const ranked = names
    .filter((name) => version(name) >= 0)
    .sort((a, b) => version(b) - version(a) || coder(b) - coder(a) || a.localeCompare(b));
  return ranked[0] ?? names.find((name) => name.startsWith("qwen")) ?? names[0];
}
