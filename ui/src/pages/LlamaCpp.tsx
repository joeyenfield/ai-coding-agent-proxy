import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  fetchProviderLog,
  type FleetMachine,
  type LlamaInstallJob,
  type LlamaModel,
  type LlamaServerState,
  type LlamaSettings,
  type LlamaState,
  type OllamaConfig,
  type ProviderRef,
} from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ALL_LINES, LogPanel, PROBLEMS, quietView, type LogTone } from "../components/LogPanel";
import { ModelSearch, PullStatus, pullPercent, usePull } from "../components/ModelSearch";
import { TabBar, TabCount, TabPanel, UnsavedDot, useTabParam } from "../components/Tabs";
import { formatBytes, formatNumber } from "../format";
import { useToast } from "../hooks";
import { EnvironmentTab, SettingsTab, type EnvironmentText, type Value } from "./OllamaServer";

const POLL_MS = 3000;
const MODEL_TABS = ["models", "find"] as const;
const SERVER_TABS = ["settings", "environment", "log"] as const;
/** Where agent-machine runs llama-server unless the machine says otherwise. */
const DEFAULT_PORT = 9931;

const STATE_LABELS: Record<string, string> = {
  loaded: "Loaded",
  loading: "Loading…",
  unloaded: "Not loaded",
  sleeping: "Asleep (wakes on the next request)",
  downloading: "Downloading…",
  incomplete: "Download not finished",
};

/**
 * One llama.cpp on one machine: the models its router can serve (load, unload, delete) and
 * downloading more from Hugging Face. The machine does all of it (agent-machine), through
 * llama-server's own /models API. manageable, when set, says why changes can't be made now.
 */
export function LlamaCppProvider({ target, label, manageable }: { target: ProviderRef; label: string; backend: string | null; manageable: string | null }) {
  const key = ["provider", target.machine, target.provider];
  const state = useQuery({ queryKey: key, queryFn: () => api.providerStatus<LlamaState>(target), refetchInterval: POLL_MS });
  const client = useQueryClient();
  const [tab, setTab] = useTabParam(MODEL_TABS);
  const tabs = useId();
  const puller = usePull(target, label, () => {
    client.invalidateQueries({ queryKey: key });
    client.invalidateQueries({ queryKey: ["models"] });
  });
  const percent = pullPercent(puller.pull?.progress ?? null);

  if (state.isPending) return <p className="empty">Asking {label} for its models…</p>;
  if (state.isError) return <p className="bad">{state.error.message}</p>;
  const data = state.data;
  const fixed = data.online && !data.router ? "This llama-server serves one model (it was started with -m), so its models can't change from here. Start llama.cpp from its Server settings on this machine to serve many." : null;
  return (
    <>
      <LlamaTotals state={data} />
      <div className="tabbed">
        <TabBar
          label="llama.cpp sections"
          idPrefix={tabs}
          className="page-tabs"
          active={tab}
          onSelect={setTab}
          tabs={[
            { id: "models", label: "Models", badge: <TabCount value={data.models.length} /> },
            {
              id: "find",
              label: "Find and download",
              badge: puller.active ? <TabCount tone="live" value={percent != null ? `${percent}%` : "…"} /> : undefined,
            },
          ]}
        />
        <PullStatus puller={puller} />
        <TabPanel idPrefix={tabs} id="models" active={tab === "models"} keepMounted>
          <LlamaModels state={data} target={target} label={label} manageable={manageable ?? fixed} onFind={() => setTab("find")} />
        </TabPanel>
        <TabPanel idPrefix={tabs} id="find" active={tab === "find"} keepMounted>
          <ModelSearch puller={puller} flavor="llamacpp" disabled={data.online ? manageable ?? fixed : `${label} is offline.`} />
        </TabPanel>
      </div>
    </>
  );
}

function LlamaTotals({ state }: { state: LlamaState }) {
  const ready = state.models.filter((model) => model.state !== "downloading" && model.state !== "incomplete");
  const loaded = state.models.filter((model) => model.loaded);
  const traffic = state.traffic;
  return (
    <dl className="stat-cards">
      <div>
        <dt>llama.cpp</dt>
        <dd className={state.online ? undefined : "bad"}>{state.online ? (state.router ? "Router" : "One model") : "Offline"}</dd>
      </div>
      <div>
        <dt>Models</dt>
        <dd>
          {ready.length} <small>{formatBytes(ready.reduce((total, model) => total + (model.size ?? 0), 0))} on disk</small>
        </dd>
      </div>
      <div>
        <dt>Loaded now</dt>
        <dd>{loaded.length}</dd>
      </div>
      <div>
        <dt>Agent requests</dt>
        <dd>
          {traffic ? traffic.in_flight : "–"}{" "}
          <small>
            {traffic
              ? `in flight${traffic.average_tps ? `, ${formatNumber(Math.round(traffic.average_tps))} tok/s lately` : ""}`
              : "not a provider yet"}
          </small>
        </dd>
      </div>
    </dl>
  );
}

function ModelState({ model }: { model: LlamaModel }) {
  const progress = model.progress;
  const percent = progress?.total ? Math.round((progress.completed / progress.total) * 100) : null;
  const label = STATE_LABELS[model.state] ?? model.state;
  return (
    <>
      <span className={model.loaded ? "good" : model.failed ? "bad" : model.state === "downloading" || model.state === "loading" ? "live" : "subtle"}>
        {model.state === "downloading" && percent != null ? `Downloading, ${percent}%` : model.failed && !model.loaded ? "Failed to load" : label}
      </span>
      {model.context_train ? <small>Trained for {formatNumber(model.context_train)} context</small> : null}
    </>
  );
}

function LlamaModels({
  state,
  target,
  label,
  manageable,
  onFind,
}: {
  state: LlamaState;
  target: ProviderRef;
  label: string;
  manageable: string | null;
  onFind: () => void;
}) {
  const client = useQueryClient();
  const notify = useToast();
  const [query, setQuery] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const refresh = () => {
    client.invalidateQueries({ queryKey: ["provider", target.machine, target.provider] });
    client.invalidateQueries({ queryKey: ["models"] });
  };
  const act = useMutation({
    mutationFn: ({ action, model }: { action: "load" | "unload" | "delete"; model: string }) => api.providerModelAction(target, action, model),
    onSuccess: (_, { action, model }) => {
      notify(action === "load" ? `Loading ${model}` : action === "unload" ? `Unloaded ${model}` : `Deleted ${model}`);
      refresh();
    },
    onError: (error: Error) => notify(error.message),
    onSettled: () => setConfirmDelete(null),
  });
  const working = (model: string) => Boolean(manageable) || (act.isPending && act.variables?.model === model);
  const text = query.trim().toLowerCase();
  const visible = state.models.filter((model) => !text || model.name.toLowerCase().includes(text));

  if (!state.online) return <p className="bad">Offline: {state.error ?? "no answer"}</p>;
  if (!state.models.length) {
    return (
      <div className="empty">
        <p>No models on {label} yet.</p>
        {!manageable && (
          <button type="button" className="primary" onClick={onFind}>
            Find a model to download
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="tab-stack">
      {manageable && <p className="callout">{manageable}</p>}
      <p className="subtle">
        Agents name a model as it's listed here. One that isn't loaded loads on its first request; the least recently used is unloaded
        when more are loaded than the llama.cpp server's settings allow.
      </p>
      {state.models.length > 8 && (
        <div className="filters">
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter models" aria-label="Filter models" />
        </div>
      )}
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col" className="num">
                Size
              </th>
              <th scope="col">State</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((model) => {
              const busy = model.state === "downloading" || model.state === "loading";
              return (
                <tr key={model.name}>
                  <td>
                    <strong className="mono">{model.name}</strong>
                    {model.path && <small className="mono">{model.path}</small>}
                  </td>
                  <td className="num nowrap">{formatBytes(model.size)}</td>
                  <td className="nowrap">
                    <ModelState model={model} />
                  </td>
                  <td>
                    <div className="model-actions">
                      {model.loaded ? (
                        <button type="button" disabled={working(model.name)} onClick={() => act.mutate({ action: "unload", model: model.name })}>
                          Unload
                        </button>
                      ) : (
                        <button type="button" disabled={working(model.name) || busy || model.state === "incomplete"} onClick={() => act.mutate({ action: "load", model: model.name })}>
                          Load
                        </button>
                      )}
                      <button type="button" className="danger" disabled={working(model.name) || model.state === "loading"} onClick={() => setConfirmDelete(model.name)}>
                        {model.state === "downloading" ? "Cancel" : "Delete"}
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!visible.length && <p className="empty">No models match.</p>}
      </div>
      <ConfirmDialog
        open={confirmDelete !== null}
        title="Delete model?"
        confirmLabel={act.isPending ? "Deleting…" : "Delete model"}
        busy={act.isPending}
        onConfirm={() => confirmDelete && act.mutate({ action: "delete", model: confirmDelete })}
        onCancel={() => setConfirmDelete(null)}
      >
        <p>
          Delete <strong className="mono">{confirmDelete}</strong> from <strong>{label}</strong>'s cache? Files another quant of the same
          repo still uses are kept. You can download it again later. Models in its GGUF folder can only be removed there.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/** A line under each settings group; a group the machine adds later just goes without one. */
const GROUP_NOTES: Record<string, string> = {
  "Memory / KV cache": "How much context each request gets and how much memory the KV cache takes. These matter most for coding agents.",
  "Concurrency / scheduling": "How many requests and models it handles at once.",
  Hardware: "Which GPUs it uses, and how much of each model goes on them.",
  Reasoning: "How thinking models think. On a slow machine, a thinking budget is the biggest difference to how soon answers come.",
  Models: "Where downloaded models go, and a folder of GGUF files to serve as well.",
  Server: "What else llama-server offers on this computer.",
};

const LLAMA_ENV: EnvironmentText = {
  name: "llama.cpp",
  command: "llama-server",
  prefixes: (
    <>
      names starting with <code>LLAMA_ARG_</code> or <code>GGML_</code>, and none that change where it listens, give it a key, or turn on its
      tools, MCP servers or agent mode
    </>
  ),
  placeholder: "# LLAMA_ARG_BATCH=1024\n# GGML_VK_VISIBLE_DEVICES=0",
};

/** Client-side mirror of the machine's warnings so edits warn before saving. */
function warningsFor(config: OllamaConfig): string[] {
  const warnings: string[] = [];
  const context = Number(config.ctx_size) || 0;
  if (context !== 0 && context < 8192) warnings.push("Context below 8K is likely too small for coding agents.");
  else if (context !== 0 && context < 16384) warnings.push("Some coding agents may exhaust this context rapidly.");
  if (Number(config.parallel) > 1) warnings.push("Parallel requests increase KV cache memory requirements.");
  if (config.cache_type_v !== "f16" && config.cache_type_v !== "bf16" && config.flash_attn === "off") warnings.push("A quantized V cache needs Flash Attention; set it to auto or on.");
  return warnings;
}

/** The llama.cpp on a machine: installing it, start, stop, restart, the settings agent-machine starts it with, and its log. */
export function MachineLlamaCpp({ machine }: { machine: FleetMachine }) {
  if (!machine.connected) {
    return <p className="empty">{machine.name} isn't connected. Start agent-machine there (agent-machine start) to manage its llama.cpp.</p>;
  }
  if (!machine.servers.enabled) {
    return (
      <div className="callout">
        <p>
          {machine.name} doesn't let the server manage its model servers. Run <code>agent-machine servers on</code> there to install, start,
          stop and tune llama.cpp from here.
        </p>
      </div>
    );
  }
  if (machine.servers.llamacpp && !machine.servers.llamacpp.known) {
    return (
      <p className="empty">
        {machine.name}'s agent-machine is from before llama.cpp. Update it (agent-machine update, or from its Summary) to install and run
        llama.cpp from here.
      </p>
    );
  }
  return <LlamaControls machine={machine} />;
}

type Action = "start" | "stop" | "restart" | "uninstall";

function LlamaControls({ machine }: { machine: FleetMachine }) {
  const [installing, setInstalling] = useState(false);
  const settings = useQuery({
    queryKey: ["llamacpp-settings", machine.id],
    queryFn: () => api.llamaSettings(machine.id),
    // An install reports its progress here.
    refetchInterval: (query) => (query.state.data?.status?.install?.state === "installing" || installing ? 1500 : 5000),
  });
  const client = useQueryClient();
  const notify = useToast();
  const [config, setConfig] = useState<OllamaConfig | null>(null);
  const [confirm, setConfirm] = useState<Action | "save-restart" | null>(null);
  const [choosing, setChoosing] = useState(false);
  const [tab, setTab] = useTabParam(SERVER_TABS);
  const tabs = useId();
  const initialised = useRef(false);
  const lastState = useRef<string | null>(null);

  useEffect(() => {
    if (!initialised.current && settings.data) {
      setConfig(settings.data.config);
      initialised.current = true;
    }
  }, [settings.data]);

  // Say when an install finishes, and refresh what depends on it.
  const job = settings.data?.status?.install ?? null;
  useEffect(() => {
    const now = job?.state ?? null;
    if (lastState.current === "installing" && now && now !== "installing") {
      setInstalling(false);
      notify(now === "installed" ? `llama.cpp ${job?.release ?? ""} installed on ${machine.name}.` : `Installing llama.cpp failed: ${job?.error ?? "see the log"}`);
      client.invalidateQueries({ queryKey: ["machines"] });
    }
    lastState.current = now;
  }, [job, machine.name, notify, client]);

  const save = useMutation({
    mutationFn: () => api.saveLlamaSettings(machine.id, config as OllamaConfig),
    onSuccess: (info) => {
      setConfig(info.config);
      client.setQueryData(["llamacpp-settings", machine.id], info);
      notify("llama.cpp settings saved.");
    },
    onError: (error: Error) => notify(error.message),
  });
  const act = useMutation({
    mutationFn: (action: Action) => api.machineServer(machine.id, "llamacpp", action),
    onSuccess: (_, action) => {
      client.invalidateQueries({ queryKey: ["llamacpp-settings", machine.id] });
      client.invalidateQueries({ queryKey: ["llamacpp-log", machine.id] });
      client.invalidateQueries({ queryKey: ["machines"] });
      client.invalidateQueries({ queryKey: ["status"] });
      client.invalidateQueries({ queryKey: ["provider", machine.id] });
      notify(`llama.cpp ${{ start: "started", stop: "stopped", restart: "restarted", uninstall: "removed" }[action]} on ${machine.name}.`);
    },
    onError: (error: Error) => notify(error.message),
    onSettled: () => setConfirm(null),
  });
  const install = useMutation({
    mutationFn: (variant: string) => api.machineServer(machine.id, "llamacpp", "install", undefined, variant),
    onSuccess: () => {
      setInstalling(true);
      setChoosing(false);
      client.invalidateQueries({ queryKey: ["llamacpp-settings", machine.id] });
    },
    onError: (error: Error) => notify(error.message),
  });
  const saveAndRestart = async () => {
    try {
      await save.mutateAsync();
    } catch {
      setConfirm(null);
      return;
    }
    act.mutate("restart");
  };

  if (settings.isPending) return <p className="empty">Asking {machine.name} about its llama.cpp…</p>;
  if (settings.isError) return <p className="bad">{(settings.error as Error).message}</p>;

  const info = settings.data as LlamaSettings;
  const status: LlamaServerState = info.status ?? machine.servers.llamacpp ?? { known: true, found: false, running: false, managed: false };
  const owner = Boolean(machine.you.servers);
  const busy = act.isPending;
  const changed = (key: string) => config != null && config[key] !== info.config[key];
  const changedGroups = info.schema.groups.filter((group) => info.schema.settings.some((setting) => setting.group === group && changed(setting.key)));
  const envChanged = changed("extra_env");
  const dirty = changedGroups.length > 0 || envChanged;
  const warnings = config ? warningsFor(config) : info.warnings;
  const update = (key: string, value: Value) => setConfig((current) => (current ? { ...current, [key]: value } : current));
  const canRestart = owner && status.managed && status.running;
  const port = status.port ?? DEFAULT_PORT;
  const inProgress = status.install?.state === "installing" || install.isPending;

  return (
    <>
      <section className={`status-card is-${busy || inProgress ? "busy" : status.running ? "running" : "stopped"}`} aria-label="llama.cpp status">
        <div className="status-card-head">
          <h2>
            {status.version ? `llama.cpp ${status.version}` : "llama.cpp"}
            <span className={`pill ${busy || inProgress ? "live" : status.running ? "good" : ""}`}>
              {inProgress ? "Installing…" : busy ? "Working…" : status.running ? "Running" : status.found ? "Stopped" : "Not installed"}
            </span>
          </h2>
          <div className="row">
            <button type="button" className="primary" disabled={!owner || busy || inProgress || status.running || !status.found} onClick={() => act.mutate("start")}>
              Start
            </button>
            <button type="button" disabled={!owner || busy || inProgress || !status.found || (status.running && !status.managed)} onClick={() => setConfirm("restart")}>
              Restart
            </button>
            <button type="button" className="danger" disabled={!owner || busy || !(status.running && status.managed)} onClick={() => setConfirm("stop")}>
              Stop
            </button>
          </div>
        </div>
        <dl className="fact-list">
          <div>
            <dt>Machine</dt>
            <dd>{machine.name}</dd>
          </div>
          <div>
            <dt>Listens on</dt>
            <dd className="mono">127.0.0.1:{port}</dd>
          </div>
          <div>
            <dt>Installed</dt>
            <dd>
              {!status.found
                ? "No"
                : status.source === "termux"
                  ? `Termux's packages (${status.variant})`
                  : status.source === "github"
                    ? `${status.release} (${status.variant}), from GitHub`
                    : "Found there"}
            </dd>
          </div>
          <div>
            <dt>Started by</dt>
            <dd>{!status.running ? "–" : status.managed ? "agent-machine" : "Outside agent-machine"}</dd>
          </div>
        </dl>
        <InstallProgress job={status.install ?? null} />
        {!inProgress && (
          <div className="row wrap">
            <button type="button" className={status.found ? undefined : "primary"} disabled={!owner} onClick={() => setChoosing((open) => !open)}>
              {status.found ? (status.source === "found" ? "Install llama.cpp's own build" : "Update or change build") : "Install llama.cpp"}
            </button>
            {status.source === "github" && (
              <button type="button" className="danger" disabled={!owner || status.managed} onClick={() => setConfirm("uninstall")}>
                Remove
              </button>
            )}
            {!owner && <span className="subtle">Installing and starting it needs the machine's servers permission.</span>}
          </div>
        )}
        {choosing && !inProgress && <InstallChooser machine={machine} busy={install.isPending} onInstall={(variant) => install.mutate(variant)} />}
        {status.running && !status.managed && (
          <div className="callout">
            <p>
              A llama-server agent-machine didn't start answers on port {port}, so it can't be stopped from here and these settings don't apply
              to it. Stop it on {machine.name}, then press Start here.
            </p>
          </div>
        )}
        {status.running && info.applied_matches_saved === false && (
          <div className="callout">
            <p className="warn">The running server was started with different settings. Restart to apply the saved configuration.</p>
          </div>
        )}
      </section>

      <div className="tabbed">
        <TabBar
          label="llama.cpp server sections"
          idPrefix={tabs}
          className="page-tabs"
          active={tab}
          onSelect={setTab}
          tabs={[
            { id: "settings", label: "Settings", badge: changedGroups.length > 0 ? <UnsavedDot /> : undefined },
            { id: "environment", label: "Environment", badge: envChanged ? <UnsavedDot /> : undefined },
            { id: "log", label: "Log" },
          ]}
        />
        <TabPanel idPrefix={tabs} id="settings" active={tab === "settings"}>
          {config && <SettingsTab info={info} config={config} warnings={warnings} changed={changed} onChange={update} notes={GROUP_NOTES} />}
        </TabPanel>
        <TabPanel idPrefix={tabs} id="environment" active={tab === "environment"}>
          {config && <EnvironmentTab info={info} config={config} dirty={dirty} onChange={update} text={LLAMA_ENV} />}
        </TabPanel>
        <TabPanel idPrefix={tabs} id="log" active={tab === "log"}>
          <LogPanel
            queryKey={["llamacpp-log", machine.id]}
            load={() => fetchProviderLog({ machine: machine.id, provider: `llamacpp-${port}` })}
            tone={llamaTone}
            views={LLAMA_LOG_VIEWS}
            defaultView="quiet"
            empty="No log yet. It covers a llama-server agent-machine started."
            footer={<>From <code>{info.log_path}</code> on {machine.name}</>}
          />
        </TabPanel>
      </div>

      {tab !== "log" && config && (
        <div className="save-bar">
          <p className={dirty ? "warn" : "subtle"}>
            {!owner
              ? `Only ${machine.owner ?? "its owner"}, or those it lets, can change ${machine.name}'s llama.cpp.`
              : dirty
                ? `Unsaved changes in ${[...changedGroups, ...(envChanged ? ["Environment"] : [])].join(", ")}.`
                : info.applied_matches_saved === false
                  ? "Saved. Restart llama.cpp to apply the saved settings."
                  : <>Saved to <code>{info.config_path}</code> on {machine.name}</>}
          </p>
          <div className="row">
            {dirty && (
              <button type="button" disabled={save.isPending} onClick={() => setConfig(info.config)}>
                Revert
              </button>
            )}
            <button type="button" className={canRestart ? undefined : "primary"} disabled={!owner || !dirty || save.isPending} onClick={() => save.mutate()}>
              {save.isPending && confirm !== "save-restart" ? "Saving…" : "Save"}
            </button>
            {canRestart && (
              <button type="button" className="primary" disabled={!dirty || busy || save.isPending} onClick={() => setConfirm("save-restart")}>
                Save and restart
              </button>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirm === "stop"}
        title="Stop llama.cpp?"
        confirmLabel={act.isPending ? "Stopping…" : "Stop server"}
        busy={act.isPending}
        onConfirm={() => act.mutate("stop")}
        onCancel={() => setConfirm(null)}
      >
        <p>This stops the llama-server agent-machine started on {machine.name}, and every model it has loaded. Running agents' requests will be interrupted.</p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirm === "restart" || confirm === "save-restart"}
        title={confirm === "save-restart" ? "Save and restart llama.cpp?" : "Restart llama.cpp?"}
        confirmLabel={save.isPending ? "Saving…" : act.isPending ? "Restarting…" : "Restart server"}
        busy={save.isPending || act.isPending}
        onConfirm={() => (confirm === "save-restart" ? void saveAndRestart() : act.mutate("restart"))}
        onCancel={() => setConfirm(null)}
      >
        <p>
          {confirm === "save-restart" ? "This saves your changes, then stops" : "This stops"} the running server and starts it again with the
          saved settings (and the build installed last). In-flight requests will be interrupted.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirm === "uninstall"}
        title="Remove llama.cpp?"
        confirmLabel={act.isPending ? "Removing…" : "Remove"}
        busy={act.isPending}
        onConfirm={() => act.mutate("uninstall")}
        onCancel={() => setConfirm(null)}
      >
        <p>
          This deletes the llama.cpp builds installed from here on {machine.name}. Downloaded models stay in their folder, and its settings are
          kept.
        </p>
      </ConfirmDialog>
    </>
  );
}

function InstallProgress({ job }: { job: LlamaInstallJob | null }) {
  if (!job?.state || job.state === "installed") return null;
  if (job.state === "failed") {
    return (
      <div className="callout">
        <p className="bad">Installing llama.cpp ({job.variant}) failed: {job.error}</p>
      </div>
    );
  }
  const percent = job.total ? Math.round((job.done / job.total) * 100) : null;
  return (
    <div className="pull callout" role="status">
      <p>
        Installing llama.cpp {job.release ?? ""} ({job.variant}){job.step ? `: ${job.step}` : "…"}
        {percent != null && job.done > 0 ? ` (${formatBytes(job.done)} of ${formatBytes(job.total)})` : ""}
      </p>
      {percent != null && (
        <div className="pull-bar">
          <span style={{ width: `${percent}%` }} />
        </div>
      )}
    </div>
  );
}

/** The builds llama.cpp has for the machine, as the machine finds them, to pick one to install. */
function InstallChooser({ machine, busy, onInstall }: { machine: FleetMachine; busy: boolean; onInstall: (variant: string) => void }) {
  const options = useQuery({ queryKey: ["llamacpp-install", machine.id], queryFn: () => api.llamaInstallOptions(machine.id), staleTime: 60_000 });
  const [picked, setPicked] = useState<string | null>(null);
  if (options.isPending) return <p className="subtle">Asking {machine.name} which builds fit it…</p>;
  if (options.isError) return <p className="bad">{(options.error as Error).message}</p>;
  const data = options.data;
  if (data.error) return <p className="bad">{data.error}</p>;
  const chosen = picked ?? data.variants.find((variant) => variant.recommended)?.id ?? data.variants[0]?.id ?? null;
  return (
    <div className="panel install-chooser">
      <p className="subtle">
        {data.source === "termux"
          ? "From Termux's own packages, with apt. The GPU builds use the phone's own drivers."
          : `llama.cpp ${data.release}, published by ggml-org on GitHub. agent-machine checks the download's SHA-256 and unpacks it into its own folder; nothing else on ${machine.name} changes.`}
      </p>
      <fieldset className="choice-list">
        <legend>Build</legend>
        {data.variants.map((variant) => (
          <label key={variant.id} className="check">
            <input type="radio" name={`llama-variant-${machine.id}`} checked={chosen === variant.id} onChange={() => setPicked(variant.id)} />
            <span>
              <strong>{variant.label}</strong>
              {variant.recommended && <span className="pill good">Suggested</span>}
              {variant.size ? <span className="subtle"> · {formatBytes(variant.size)}</span> : null}
              {variant.help && <small>{variant.help}</small>}
            </span>
          </label>
        ))}
      </fieldset>
      <div className="row">
        <button type="button" className="primary" disabled={!chosen || busy} onClick={() => chosen && onInstall(chosen)}>
          {busy ? "Starting…" : "Install"}
        </button>
      </div>
    </div>
  );
}

/** llama-server's log: errors and warnings stand out, the per-request lines can be hidden. */
function llamaTone(line: string): LogTone {
  if (/\berror\b|failed|abort|GGML_ASSERT/i.test(line)) return "bad";
  if (/\bwarn(ing)?\b/i.test(line)) return "warn";
  if (/^(srv|slot)\s+(log_server_r|update_slots|print_timing|launch_slot_|get_availabl|release)/.test(line) || /request: (GET|POST) /.test(line)) return "muted";
  return "";
}

const LLAMA_LOG_VIEWS = [ALL_LINES, quietView("Hide request lines"), PROBLEMS];
