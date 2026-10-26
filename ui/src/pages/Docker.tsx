import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { api, PROTOCOL_LABELS, type DockerHubSearch, type DockerState, type ProviderRef } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PullStatus, pullPercent, usePull, type Puller } from "../components/ModelSearch";
import { TabBar, TabCount, TabPanel, useTabParam } from "../components/Tabs";
import { formatBytes, formatNumber, formatRelative } from "../format";
import { useAgents, useClipboard, useStatus, useToast } from "../hooks";

const TABS = ["models", "get", "connect"] as const;
const POLL_MS = 3000;
const DOCS = "https://docs.docker.com/ai/model-runner/";
const ENABLE_TCP = "docker desktop enable model-runner --tcp 12434";

/** A Docker backend's state from its machine, with the backend's name and where it runs. */
type DockerView = DockerState & { name: string; backend: string | null; target: ProviderRef; manageable: string | null };

type ModelAction = "load" | "unload" | "delete";

/** Docker Hub tags that don't run on llama.cpp, the engine every Model Runner has. */
const ENGINE_NOTES = {
  vllm: "Safetensors weights: needs Model Runner's vLLM engine (Linux with an NVIDIA GPU)",
  mlx: "MLX weights: needs Model Runner's MLX engine (Apple silicon)",
};

/** How to get a machine's Model Runner into the fleet, for a machine that doesn't report one. */
export function DockerGuide() {
  const copy = useClipboard();
  return (
    <section className="panel" aria-labelledby="connect-title">
      <h2 id="connect-title">Add Model Runner from a machine</h2>
      <p>Model Runner is a provider like the others: it runs on one of your machines, which supplies it to the server.</p>
      <ol className="steps">
        <li>
          <strong>Install Docker on the machine.</strong> Docker Desktop includes Model Runner. On Linux, Docker Engine with the{" "}
          <code>docker-model-plugin</code> package works too.
        </li>
        <li>
          <strong>Turn on Model Runner's TCP port.</strong> Docker Desktop keeps it off until you run this, or turn on host-side
          TCP support in its Model Runner settings. Docker Engine listens on 12434 already.
          <CodeLine text={ENABLE_TCP} onCopy={() => copy(ENABLE_TCP, "Command copied")} />
        </li>
        <li>
          <strong>Sign the machine in</strong> with <code>agent-machine login</code> and start it. agent-machine finds Model Runner, and{" "}
          <Link to="/machines">Machines</Link> offers to add it as a provider.
        </li>
      </ol>
      <p className="subtle">
        See the{" "}
        <a href={DOCS} target="_blank" rel="noreferrer">
          Model Runner documentation
        </a>{" "}
        for setup.
      </p>
    </section>
  );
}

/**
 * One Docker Model Runner on one machine: its models (pull, load, unload, delete), Docker Hub,
 * and the agents that can use it, all through the machine. backend is the backend agents use
 * it as (null until it's added); manageable, when set, says why it can't be changed now.
 */
export function DockerProvider({ target, label, backend, manageable }: { target: ProviderRef; label: string; backend: string | null; manageable: string | null }) {
  const name = backend ?? label;
  const state = useQuery({
    queryKey: ["provider", target.machine, target.provider],
    queryFn: () => api.providerStatus<DockerState>(target),
    refetchInterval: POLL_MS,
  });
  const client = useQueryClient();
  const puller = usePull(target, label, () => {
    client.invalidateQueries({ queryKey: ["provider", target.machine, target.provider] });
    client.invalidateQueries({ queryKey: ["models"] });
  });
  const [tab, setTab] = useTabParam(TABS);
  const tabs = useId();

  if (state.isPending) return <p className="empty">Asking the machine that runs {name}…</p>;
  if (state.isError) return <p className="bad">{state.error.message}</p>;
  const data: DockerView = { ...state.data, name, backend, target, manageable };
  const percent = pullPercent(puller.pull?.progress ?? null);
  return (
    <>
      {manageable && <p className="callout">{manageable}</p>}
      <StatusCard state={data} />
      <div className="tabbed">
        <TabBar
          label="Docker sections"
          idPrefix={tabs}
          className="page-tabs"
          active={tab}
          onSelect={setTab}
          tabs={[
            { id: "models", label: "Models", badge: <TabCount value={data.models.length} /> },
            {
              id: "get",
              label: "Get models",
              badge: puller.active ? <TabCount tone="live" value={percent != null ? `${percent}%` : "…"} /> : undefined,
            },
            { id: "connect", label: "Connect agents" },
          ]}
        />
        <PullStatus puller={puller} />
        <TabPanel idPrefix={tabs} id="models" active={tab === "models"}>
          <ModelsTab state={data} onGet={() => setTab("get")} />
        </TabPanel>
        <TabPanel idPrefix={tabs} id="get" active={tab === "get"}>
          <GetModelsTab state={data} puller={puller} />
        </TabPanel>
        <TabPanel idPrefix={tabs} id="connect" active={tab === "connect"}>
          <ConnectTab state={data} />
        </TabPanel>
      </div>
    </>
  );
}

/** "running llama.cpp latest-cuda (sha256:…) version: b6000" reads as "running". */
const engineState = (description: string) => (description.startsWith("running") ? "running" : description);

function StatusCard({ state }: { state: DockerView }) {
  const traffic = state.traffic ?? { in_flight: 0, recent_requests: 0, average_tps: null };
  const loaded = state.models.filter((model) => model.loaded || model.loading);
  const engines = Object.entries(state.engines);
  return (
    <section className={`status-card ${state.online ? "is-running" : ""}`} aria-label="Docker Model Runner status">
      <div className="status-card-head">
        <h2>
          {state.name}
          <span className={`pill ${state.online ? "good" : "bad"}`}>{state.online ? "Online" : "Offline"}</span>
        </h2>
      </div>
      <dl className="fact-list">
        <div>
          <dt>Address</dt>
          <dd>
            <span className="mono">{state.url}</span>
          </dd>
        </div>
        <div>
          <dt>Models pulled</dt>
          <dd>{state.online ? formatNumber(state.models.length) : "–"}</dd>
        </div>
        <div>
          <dt>Disk used</dt>
          <dd>{formatBytes(state.disk?.models_disk_usage)}</dd>
        </div>
        <div>
          <dt>Requests in flight</dt>
          <dd>
            {formatNumber(traffic.in_flight)}
            {traffic.average_tps != null && <small>{formatNumber(traffic.average_tps)} tok/s lately</small>}
          </dd>
        </div>
        <div className="wide">
          <dt>Loaded now</dt>
          <dd className="mono">{loaded.length ? loaded.map((model) => model.name).join(", ") : <span className="subtle">None</span>}</dd>
        </div>
        {engines.length > 0 && (
          <div className="wide">
            <dt>Engines</dt>
            <dd>
              {engines.map(([engine, description], index) => (
                <span key={engine} title={description}>
                  {index > 0 && ", "}
                  {engine} <span className={description.startsWith("running") ? "good" : "subtle"}>{engineState(description)}</span>
                </span>
              ))}
            </dd>
          </div>
        )}
      </dl>
      {!state.online && (
        <div className="callout">
          <p className="bad">{state.error}</p>
          <p>
            Start Docker on the machine and check that Model Runner's TCP port is on: <code>{ENABLE_TCP}</code>
          </p>
        </div>
      )}
      {state.online && state.error && <p className="warn">{state.error}</p>}
    </section>
  );
}

function ModelsTab({ state, onGet }: { state: DockerView; onGet: () => void }) {
  const client = useQueryClient();
  const notify = useToast();
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: ({ action, model }: { action: ModelAction; model: string }) => api.providerModelAction(state.target, action, model),
    onSuccess: (_, { action, model }) => {
      notify(action === "load" ? `Loaded ${model}.` : action === "unload" ? `Unloaded ${model}; its memory is free.` : `Deleted ${model}.`);
      client.invalidateQueries({ queryKey: ["provider", state.target.machine, state.target.provider] });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error: Error) => notify(error.message),
    onSettled: () => setConfirmDelete(null),
  });
  const working = (model: string) => Boolean(state.manageable) || (act.isPending && act.variables?.model === model);

  if (!state.online) return <p className="empty">Model Runner is offline.</p>;
  if (!state.models.length) {
    return (
      <div className="empty">
        <p>No models pulled yet.</p>
        <button type="button" className="primary" onClick={onGet}>
          Find a model to pull
        </button>
      </div>
    );
  }
  return (
    <div className="tab-stack">
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Name agents use</th>
              <th scope="col">Parameters</th>
              <th scope="col">Quantization</th>
              <th scope="col" className="num">Size</th>
              <th scope="col" className="num">Context</th>
              <th scope="col">State</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {state.models.map((model) => {
              const others = model.tags.filter((tag) => tag !== model.name);
              return (
                <tr key={model.id}>
                  <td>
                    <strong className="mono">{model.name}</strong>
                    {others.length > 0 && <small className="mono">Also {others.join(", ")}</small>}
                  </td>
                  <td>
                    {model.parameters ?? "–"}
                    {model.architecture && <small>{model.architecture}</small>}
                  </td>
                  <td>
                    {model.quantization ?? "–"}
                    {model.format && <small>{model.format}</small>}
                  </td>
                  <td className="num">{model.size ?? "–"}</td>
                  <td className="num">{formatNumber(model.context_size)}</td>
                  <td>
                    {model.loading ? (
                      <span className="warn">Loading…</span>
                    ) : model.loaded ? (
                      <span className="good">Loaded{model.in_use ? ", in use" : ""}</span>
                    ) : (
                      <span className="subtle">On disk</span>
                    )}
                  </td>
                  <td>
                    <div className="model-actions">
                      {model.loaded ? (
                        <button type="button" disabled={working(model.name)} onClick={() => act.mutate({ action: "unload", model: model.name })}>
                          Unload
                        </button>
                      ) : (
                        <button type="button" disabled={working(model.name) || model.loading} onClick={() => act.mutate({ action: "load", model: model.name })}>
                          {working(model.name) && act.variables?.action === "load" ? "Loading…" : "Load"}
                        </button>
                      )}
                      <button type="button" className="danger" disabled={working(model.name)} onClick={() => setConfirmDelete(model.name)}>
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="subtle">
        Model Runner loads a model when a request first names it and unloads idle ones on its own. <strong>Load</strong> warms
        one up before an agent starts; <strong>Unload</strong> frees its memory now.
      </p>
      <ConfirmDialog
        open={confirmDelete != null}
        title={`Delete ${confirmDelete ?? ""}?`}
        confirmLabel={act.isPending ? "Deleting…" : "Delete model"}
        busy={act.isPending}
        onConfirm={() => confirmDelete && act.mutate({ action: "delete", model: confirmDelete })}
        onCancel={() => setConfirmDelete(null)}
      >
        <p>This removes the model from {state.name}. Pull it again to use it.</p>
      </ConfirmDialog>
    </div>
  );
}

function GetModelsTab({ state, puller }: { state: DockerView; puller: Puller }) {
  const [ref, setRef] = useState("");
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  // The machine searches Docker Hub, as it's the one that pulls.
  const machine = state.target.machine;
  const search = useQuery({ queryKey: ["docker-hub", machine, query], queryFn: () => api.searchDockerHub(machine, query), staleTime: 5 * 60_000 });
  const pull = (name: string) => (state.online && !state.manageable ? puller.start(name) : Promise.resolve(false));
  const pullByName = async (event: FormEvent) => {
    event.preventDefault();
    if (await pull(ref.trim())) setRef("");
  };
  return (
    <div className="tab-stack">
      <section className="section">
        <h2>Pull by name</h2>
        <form className="filters pull-form" onSubmit={(event) => void pullByName(event)}>
          <input
            value={ref}
            onChange={(event) => setRef(event.target.value)}
            placeholder="ai/qwen3:8B-Q4_K_M, or hf.co/<user>/<repo>:Q4_K_M"
            aria-label="Model to pull"
          />
          <button type="submit" className="primary" disabled={!ref.trim() || puller.active}>
            Pull to {state.name}
          </button>
        </form>
        <p className="subtle">
          Any model on Docker Hub, or a GGUF repo on Hugging Face as <code>hf.co/user/repo</code> with an optional{" "}
          <code>:quant</code>.
        </p>
      </section>
      <section className="section">
        <h2>Docker Hub</h2>
        <form
          className="filters"
          onSubmit={(event) => {
            event.preventDefault();
            setQuery(input.trim());
          }}
        >
          <input type="search" value={input} onChange={(event) => setInput(event.target.value)} placeholder="Search Docker's models by name, e.g. qwen" aria-label="Search Docker Hub models" />
          <button type="submit">Search</button>
        </form>
        <HubResults machine={machine} result={search} busy={puller.active} onPull={(name) => void pull(name)} />
      </section>
    </div>
  );
}

function HubResults({ machine, result, busy, onPull }: { machine: string; result: UseQueryResult<DockerHubSearch>; busy: boolean; onPull: (name: string) => void }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  if (result.isPending) return <p className="empty">Reading Docker Hub…</p>;
  if (result.isError) return <p className="bad">{result.error.message}</p>;
  if (result.data.note) return <p className="empty">{result.data.note}</p>;
  return (
    <>
      {!result.data.query && <p className="subtle">Docker's curated models, most pulled first.</p>}
      <ul className="search-results">
        {result.data.models.map((model) => (
          <li key={model.name} className="search-item">
            <div className="search-item-main">
              <button type="button" className="link mono" onClick={() => setExpanded(expanded === model.name ? null : model.name)}>
                {model.name}
              </button>
              {model.description && <small>{model.description}</small>}
              <div className="search-meta">
                {model.pulls != null && <span className="subtle">{formatNumber(model.pulls)} pulls</span>}
                {model.updated && <span className="subtle">updated {formatRelative(model.updated)}</span>}
              </div>
              {expanded === model.name && <HubTags machine={machine} repo={model.name} busy={busy} onPull={onPull} />}
            </div>
            <div className="model-actions">
              <button type="button" disabled={busy} onClick={() => onPull(model.name)}>
                Pull latest
              </button>
              <button type="button" onClick={() => setExpanded(expanded === model.name ? null : model.name)}>
                {expanded === model.name ? "Hide sizes" : "Sizes"}
              </button>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

function HubTags({ machine, repo, busy, onPull }: { machine: string; repo: string; busy: boolean; onPull: (name: string) => void }) {
  const tags = useQuery({ queryKey: ["docker-hub-tags", machine, repo], queryFn: () => api.dockerHubTags(machine, repo), staleTime: 5 * 60_000 });
  // Every Model Runner has llama.cpp; vLLM and MLX tags only run on some machines, so they're one click away.
  const [allEngines, setAllEngines] = useState(false);
  if (tags.isPending) return <p className="subtle">Reading tags…</p>;
  if (tags.isError) return <p className="bad">{tags.error.message}</p>;
  const others = tags.data.tags.filter((tag) => tag.engine).length;
  const shown = allEngines ? tags.data.tags : tags.data.tags.filter((tag) => !tag.engine);
  return (
    <div className="search-quants">
      {others > 0 && (
        <label className="check search-quants-toggle">
          <input type="checkbox" checked={allEngines} onChange={(event) => setAllEngines(event.target.checked)} />
          Also show {others} MLX and vLLM {others === 1 ? "tag" : "tags"}
        </label>
      )}
      {shown.map((tag) => (
        <button
          key={tag.tag}
          type="button"
          disabled={busy}
          className="quant"
          title={tag.engine ? ENGINE_NOTES[tag.engine] : undefined}
          onClick={() => onPull(`${repo}:${tag.tag}`)}
        >
          <span className="mono">{tag.tag}</span>
          <small>
            {formatBytes(tag.size)}
            {tag.engine === "vllm" ? " · vLLM" : tag.engine === "mlx" ? " · MLX" : ""}
          </small>
        </button>
      ))}
    </div>
  );
}

function ConnectTab({ state }: { state: DockerView }) {
  if (!state.backend) {
    return (
      <div className="callout">
        <p>Agents can use it once it's a provider: add it from its machine's overview (admins).</p>
      </div>
    );
  }
  const agents = useAgents();
  const status = useStatus();
  const copy = useClipboard();
  const model = state.models.find((item) => item.loaded)?.name ?? state.models[0]?.name ?? "<model>";
  const usable = (agents.data ?? []).filter((agent) => agent.backends?.includes(state.name));
  const proxyUrl = status.data?.proxy_url ?? "http://127.0.0.1:8181";
  const configure = `docker model configure --context-size=65536 ${model}`;
  return (
    <div className="tab-stack">
      <p>
        Every agent set up for local models works with Model Runner, including those that speak Ollama's native API. The server
        forwards their requests unchanged and records each one. Use Model Runner's model names, such as <code>ai/qwen3</code>;
        the aliases and tuning in <code>config/models.yaml</code> only apply to Ollama.
      </p>
      <div className="callout">
        <p>
          Coding agents send long prompts. If replies get cut off or forget the conversation, give the model a larger context:
        </p>
        <CodeLine text={configure} onCopy={() => copy(configure, "Command copied")} />
      </div>
      {agents.isPending && <p className="empty">Reading the agent list…</p>}
      {usable.length > 0 && (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Speaks</th>
                <th scope="col">Start it from your project folder</th>
              </tr>
            </thead>
            <tbody>
              {usable.map((agent) => {
                const command = `agent ${agent.id} . --model ${model} --backend ${state.name}`;
                return (
                  <tr key={agent.id}>
                    <td>
                      <strong>{agent.name}</strong>
                      {!agent.installed && <small className="warn">Not installed</small>}
                    </td>
                    <td>{PROTOCOL_LABELS[agent.protocol]}</td>
                    <td>
                      <CodeLine text={command} onCopy={() => copy(command, "Command copied")} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="subtle">
        Or pick {state.name} as the backend on the <Link to="/agents">Start an agent</Link> page. Any other client can send
        requests to <code>{proxyUrl}/v1</code>, <code>{proxyUrl}/anthropic</code> or <code>{proxyUrl}/api</code> with the header{" "}
        <code>X-AI-Proxy-Backend: {state.name}</code>.
      </p>
    </div>
  );
}
