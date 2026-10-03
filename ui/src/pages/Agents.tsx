import { useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Agent, type PreparedSession } from "../api";
import { useAgents, useClipboard, useModels, useStatus } from "../hooks";

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
  return (
    <div className="page wide">
      <header className="page-head">
        <h1>Agents</h1>
        <p className="lede">
          Pick a coding agent and a model. The launcher points the agent at this proxy, so every request is recorded.
        </p>
      </header>
      {agents.isError && <p className="bad">{agents.error.message}</p>}
      <div className="split">
        <ul className="agent-list split-main" aria-label="Agents">
          {list.map((agent) => (
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
                  <span className="subtle">{PROTOCOL_LABELS[agent.protocol]}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="split-side">{selected && <LaunchPanel key={selected.id} agent={selected} />}</div>
      </div>
    </div>
  );
}

function LaunchPanel({ agent }: { agent: Agent }) {
  const status = useStatus();
  const models = useModels();
  const copy = useClipboard();
  const client = useQueryClient();
  const backends = Object.keys(status.data?.backends ?? {});
  const [backend, setBackend] = useState(status.data?.default_backend ?? "");
  const [model, setModel] = useState("");
  const [trace, setTrace] = useState(false);
  const [shell, setShell] = useState<"powershell" | "bash">(navigator.userAgent.includes("Windows") ? "powershell" : "bash");
  const [prepared, setPrepared] = useState<PreparedSession | null>(null);

  useEffect(() => {
    if (!backend && status.data) setBackend(status.data.default_backend);
  }, [backend, status.data]);
  const available = useMemo(() => {
    const names = (models.data?.backends[backend]?.models ?? []).map((item) => item.name);
    return [...Object.keys(models.data?.aliases ?? {}), ...names];
  }, [models.data, backend]);
  useEffect(() => {
    if (!available.length || available.includes(model)) return;
    setModel(preferredModel(available));
  }, [available, model]);

  const prepare = useMutation({
    mutationFn: () => api.prepareAgent(agent.id, { model, backend, trace }),
    onSuccess: (result) => {
      setPrepared(result);
      client.invalidateQueries({ queryKey: ["sessions"] });
    },
  });
  const launcher = ["agent", agent.id, ".", "--model", model || "<model>", ...(backend && backend !== status.data?.default_backend ? ["--backend", backend] : []), ...(trace ? ["--trace"] : [])].join(" ");
  const backendState = models.data?.backends[backend];

  return (
    <section className="panel" aria-labelledby="launch-title">
      <h2 id="launch-title">Start {agent.name}</h2>
      {!agent.installed && agent.install && (
        <div className="callout">
          <p>
            <code>{agent.executable}</code> isn't on this machine's PATH. Install it first:
          </p>
          <CodeLine text={agent.install} onCopy={() => copy(agent.install!, "Install command copied")} />
        </div>
      )}
      <div className="form-grid two">
        <label className="field">
          Backend
          <select value={backend} onChange={(event) => setBackend(event.target.value)}>
            {backends.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <label className="field">
          Model
          <select value={model} onChange={(event) => setModel(event.target.value)} disabled={!available.length}>
            {available.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
      </div>
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
