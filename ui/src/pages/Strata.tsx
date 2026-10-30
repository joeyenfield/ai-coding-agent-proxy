import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fetchProviderLog, PROTOCOL_LABELS, type ProviderRef, type StrataInstance, type StrataLog, type StrataState } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ALL_LINES, LogPanel, PROBLEMS, quietView, type LogTone } from "../components/LogPanel";
import { Meter } from "../components/Meter";
import { TabBar, TabPanel, useTabParam } from "../components/Tabs";
import { formatNumber } from "../format";
import { useAgents, useClipboard, useStatus, useToast } from "../hooks";

const TABS = ["overview", "connect", "log"] as const;
const POLL_MS = 3000;
const GIB = 1024;

/** Strata's server log: one progress line every second or so per request, then a "done" summary. */
function strataTone(line: string): LogTone {
  if (/\b(error|exception|traceback|failed|fatal)\b/i.test(line)) return "bad";
  if (/\bwarn(ing)?\b/i.test(line)) return "warn";
  if (/^\[strata\] (thinking|reading the prompt|writing|answering)\b/.test(line)) return "muted";
  return "";
}

const STRATA_LOG_VIEWS = [ALL_LINES, quietView("Hide progress lines"), PROBLEMS];

function formatUptime(seconds: number | undefined): string {
  if (seconds == null) return "–";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return `${Math.round(seconds)} s`;
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ${minutes % 60} min`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

const rate = (value: number | undefined) => (value == null ? "–" : formatNumber(Math.round(value)));

/**
 * One Strata on one machine: its model, key, speed and GPU, loading and unloading it, the
 * agents that can use it and its logs, all from the machine. backend is the backend agents
 * use it as (null until it's added); manageable, when set, says why it can't be changed now.
 */
export function StrataProvider({ target, label, backend, manageable }: { target: ProviderRef; label: string; backend: string | null; manageable: string | null }) {
  const name = backend ?? label;
  const key = ["provider", target.machine, target.provider];
  const state = useQuery({ queryKey: key, queryFn: () => api.providerStatus<StrataState>(target), refetchInterval: POLL_MS });
  const [tab, setTab] = useTabParam(TABS);
  const tabs = useId();
  const client = useQueryClient();
  const notify = useToast();
  const act = useMutation({
    mutationFn: (action: "load" | "unload") => api.providerModelAction(target, action),
    onSuccess: (_, action) => {
      notify(action === "load" ? "Model loaded." : "Model unloaded. Its GPU memory is free until the next request loads it.");
      client.invalidateQueries({ queryKey: key });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error: Error) => notify(error.message),
  });

  if (state.isPending) return <p className="empty">Asking the machine that runs {name}…</p>;
  if (state.isError) return <p className="bad">{(state.error as Error).message}</p>;
  // An offline Strata answers without a key or models; the cards still need both.
  const data: StrataState = { ...state.data, key: state.data.key ?? { state: "unknown", source: null }, models: state.data.models ?? [] };
  return (
    <>
      {manageable && <p className="callout">{manageable}</p>}
      <StatusCard state={data} busy={act.isPending || Boolean(manageable)} onAction={(action) => act.mutate(action)} />
      <div className="tabbed">
        <TabBar
          label="Strata sections"
          idPrefix={tabs}
          className="page-tabs"
          active={tab}
          onSelect={setTab}
          tabs={[
            { id: "overview", label: "Overview" },
            { id: "connect", label: "Connect agents" },
            { id: "log", label: "Log" },
          ]}
        />
        <TabPanel idPrefix={tabs} id="overview" active={tab === "overview"}>
          <OverviewTab state={data} />
        </TabPanel>
        <TabPanel idPrefix={tabs} id="connect" active={tab === "connect"}>
          {backend ? <ConnectTab state={data} name={backend} /> : NOT_ADOPTED}
        </TabPanel>
        <TabPanel idPrefix={tabs} id="log" active={tab === "log"}>
          {data.instance ? (
            <StrataLogs target={target} instance={data.instance} />
          ) : (
            <p className="empty">No saved Strata profile on its machine serves on port {data.port}, so there's no log to read.</p>
          )}
        </TabPanel>
      </div>
    </>
  );
}

const NOT_ADOPTED = (
  <div className="callout">
    <p>Agents can use it once it's a provider: add it from its machine's overview (admins).</p>
  </div>
);

const KEY_LABELS: Record<StrataState["key"]["state"], string> = {
  ok: "In use",
  not_required: "Not required",
  missing: "Not found",
  rejected: "Rejected",
  unknown: "–",
};

function keySource(source: string | null): string {
  if (source === "environment") return "STRATA_API_KEY";
  if (source === "container") return "the key the server gave its container";
  return source?.replace(/^profile /, "Strata profile ") ?? "";
}

function StatusCard({ state, busy, onAction }: { state: StrataState; busy: boolean; onAction: (action: "load" | "unload") => void }) {
  const status = state.status;
  const loaded = status?.loaded ?? state.health?.loaded ?? false;
  const model = status?.model ?? state.health?.model ?? state.instance?.model;
  const keyBlocked = state.key.state === "missing" || state.key.state === "rejected";
  return (
    <section className={`status-card ${state.online && loaded ? "is-running" : ""}`} aria-label="Strata status">
      <div className="status-card-head">
        <h2>
          {model ?? "Strata"}
          <span className={`pill ${!state.online ? "bad" : loaded ? "good" : ""}`}>
            {!state.online ? "Offline" : loaded ? "Loaded" : "Unloaded"}
          </span>
        </h2>
        <div className="row">
          {state.online &&
            (loaded ? (
              <button type="button" disabled={busy || keyBlocked} onClick={() => onAction("unload")} title="Free the GPU now; the next request loads the model again">
                Unload
              </button>
            ) : (
              <button type="button" className="primary" disabled={busy || keyBlocked} onClick={() => onAction("load")}>
                Load
              </button>
            ))}
          <a className="button" href={state.url} target="_blank" rel="noreferrer">
            Open Strata
          </a>
        </div>
      </div>
      <dl className="fact-list">
        <div>
          <dt>Address</dt>
          <dd className="mono">{state.url}</dd>
        </div>
        <div>
          <dt>Engine</dt>
          <dd>{status?.engine ? `Strata ${status.engine}` : "–"}</dd>
        </div>
        <div>
          <dt>Context</dt>
          <dd>{formatNumber(status?.context?.native ?? state.health?.max_context)} tokens</dd>
        </div>
        <div>
          <dt>Up for</dt>
          <dd>{formatUptime(status?.uptime_s)}</dd>
        </div>
        <div>
          <dt>API key</dt>
          <dd className={keyBlocked ? "bad" : undefined}>
            {KEY_LABELS[state.key.state]}
            {state.key.source && state.key.state !== "missing" && <small>from {keySource(state.key.source)}</small>}
          </dd>
        </div>
        <div>
          <dt>Profile</dt>
          <dd>{state.instance ? state.instance.profile : "No saved profile"}</dd>
        </div>
      </dl>
      {!state.online && (
        <div className="callout">
          <p className="bad">{state.error}</p>
          <p>Once Strata is running on its machine, this page picks it up.</p>
        </div>
      )}
      {state.key.state === "missing" && (
        <div className="callout">
          <p className="warn">
            Strata wants an API key and its machine couldn't find one, so it will refuse agents' requests. Set{" "}
            <code>STRATA_API_KEY</code> for agent-machine there, or serve it from a saved Strata profile on that machine.
          </p>
        </div>
      )}
      {state.key.state === "rejected" && (
        <div className="callout">
          <p className="warn">
            Strata refused the key from {keySource(state.key.source)}. If you changed Strata's key, its machine reports the new one
            within a minute; otherwise set <code>STRATA_API_KEY</code> for agent-machine there.
          </p>
        </div>
      )}
    </section>
  );
}

function OverviewTab({ state }: { state: StrataState }) {
  const status = state.status;
  if (!state.online) return <p className="empty">Strata is offline.</p>;
  if (!status) return <p className="empty">Strata reports its speed and GPU only to clients with its API key.</p>;
  const timings = status.last_timings;
  const gpu = status.machine?.gpu;
  const ram = status.machine?.ram;
  const drafts = timings?.draft_n ? Math.round(((timings.draft_n_accepted ?? 0) / timings.draft_n) * 100) : null;
  return (
    <div className="tab-stack">
      <dl className="totals">
        <div>
          <dt>Generation</dt>
          <dd>
            {rate(timings?.predicted_per_second)} <small>tok/s</small>
          </dd>
          <small>{timings?.predicted_n != null ? `${formatNumber(timings.predicted_n)} tokens, last request` : "No request yet"}</small>
        </div>
        <div>
          <dt>Prompt reading</dt>
          <dd>
            {rate(timings?.prompt_per_second)} <small>tok/s</small>
          </dd>
          <small>
            {timings?.prompt_n != null ? `${formatNumber(timings.prompt_n)} read, ${formatNumber(timings.cache_n ?? 0)} from cache` : "–"}
          </small>
        </div>
        <div>
          <dt>Draft tokens accepted</dt>
          <dd>{drafts == null ? "–" : `${drafts}%`}</dd>
          <small>Speculative decoding, last request</small>
        </div>
        <div>
          <dt>Requests</dt>
          <dd>{formatNumber(status.activity?.requests ?? 0)}</dd>
          <small>{status.activity?.in_flight ? `${status.activity.in_flight} running now` : "since Strata started"}</small>
        </div>
      </dl>

      {(gpu || ram) && (
        <section className="section">
          <h2>Hardware</h2>
          <div className="meter-grid">
            {gpu?.total_mib != null && (
              <Meter
                label="GPU memory"
                value={gpu.used_mib ?? 0}
                max={gpu.total_mib}
                text={`${((gpu.used_mib ?? 0) / GIB).toFixed(1)} of ${(gpu.total_mib / GIB).toFixed(1)} GB`}
                detail={gpu.name}
              />
            )}
            {gpu?.util_pct != null && (
              <Meter
                label="GPU load"
                value={gpu.util_pct}
                max={100}
                text={`${Math.round(gpu.util_pct)}%`}
                detail={[gpu.temp_c != null ? `${Math.round(gpu.temp_c)}°C` : null, gpu.power_w != null ? `${Math.round(gpu.power_w)} W` : null].filter(Boolean).join(", ")}
              />
            )}
            {ram?.total_gib != null && (
              <Meter label="Memory" value={ram.used_gib ?? 0} max={ram.total_gib} text={`${(ram.used_gib ?? 0).toFixed(1)} of ${ram.total_gib.toFixed(1)} GB`} detail="Its machine" />
            )}
          </div>
        </section>
      )}

      <section className="section">
        <h2>Models</h2>
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Name agents can use</th>
                <th scope="col" className="num">Context</th>
                <th scope="col">Inputs</th>
                <th scope="col">State</th>
              </tr>
            </thead>
            <tbody>
              {state.models.map((model) => (
                <tr key={model.id}>
                  <td>
                    <strong className="mono">{model.id}</strong>
                    {model.alias_of && <small>Alias of {model.alias_of}</small>}
                  </td>
                  <td className="num">{formatNumber(model.meta?.n_ctx)}</td>
                  <td>{model.architecture?.input_modalities?.join(", ") ?? "text"}</td>
                  <td>{model.status?.value === "loaded" ? <span className="good">Loaded</span> : <span className="subtle">{model.status?.value ?? "–"}</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="subtle">
          Strata serves one model. It answers to any model name, so agents can keep the name they were started with.
          {status.dialects?.length ? ` It takes ${status.dialects.map((dialect) => dialect.replace("/v1/", "")).join(", ")} requests.` : ""}
        </p>
      </section>
    </div>
  );
}

function ConnectTab({ state, name }: { state: StrataState; name: string }) {
  const agents = useAgents();
  const status = useStatus();
  const copy = useClipboard();
  const model = state.status?.model ?? state.health?.model ?? "<model>";
  const local = (agents.data ?? []).filter((agent) => !agent.intercept && agent.backend_type === "ollama");
  const usable = local.filter((agent) => agent.backends?.includes(name));
  const native = local.filter((agent) => !usable.includes(agent));
  const proxyUrl = status.data?.proxy_url ?? "http://127.0.0.1:8181";
  return (
    <div className="tab-stack">
      <p>
        Agents set up for local models work with Strata. The server passes their OpenAI and Anthropic requests to Strata as they
        are, swaps the agent's placeholder key for Strata's, and records each one like any other request. The tuning in{" "}
        <code>config/models.yaml</code> only applies to Ollama.
      </p>
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
                const command = `agent ${agent.id} . --model ${model} --backend ${name}`;
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
      {native.length > 0 && (
        <p className="subtle">
          {native.map((agent) => agent.name).join(", ")} {native.length === 1 ? "speaks" : "speak"} Ollama's native API, which
          Strata doesn't have.
        </p>
      )}
      <p className="subtle">
        Or pick {name} as the backend on the <Link to="/agents">Start an agent</Link> page. Any other client can send
        requests to <code>{proxyUrl}/v1</code> or <code>{proxyUrl}/anthropic</code> with the header{" "}
        <code>X-AI-Proxy-Backend: {name}</code>.
      </p>
    </div>
  );
}

function StrataLogs({ target, instance }: { target: ProviderRef; instance: StrataInstance }) {
  const [source, setSource] = useState<StrataLog>(instance.logs[0] ?? "server");
  return (
    <LogPanel
      queryKey={["provider-log", target.machine, target.provider, source]}
      load={() => fetchProviderLog(target, source)}
      tone={strataTone}
      views={STRATA_LOG_VIEWS}
      defaultView="quiet"
      controls={
        instance.logs.length > 1 && (
          <select value={source} onChange={(event) => setSource(event.target.value as StrataLog)} aria-label="Which log">
            <option value="server">Server log</option>
            <option value="engine">Engine log</option>
          </select>
        )
      }
      empty="Nothing logged yet."
      footer={
        source === "server"
          ? `Requests and progress from Strata profile ${instance.profile}.`
          : `The engine's log for Strata profile ${instance.profile}: prompt speed, cache hits and drafting.`
      }
    />
  );
}
