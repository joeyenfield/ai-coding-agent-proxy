import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fetchProviderLog, type FleetMachine, type OllamaConfig, type OllamaServerState, type OllamaSetting, type OllamaSettings } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ALL_LINES, LogPanel, PROBLEMS, quietView, type LogTone } from "../components/LogPanel";
import { TabBar, TabPanel, UnsavedDot, useTabParam } from "../components/Tabs";
import { formatNumber } from "../format";
import { useClipboard, useToast } from "../hooks";

type Action = "start" | "stop" | "restart";
type Value = string | number | boolean;
const TABS = ["settings", "environment", "log"] as const;
/** Where agent-machine runs the Ollama it starts, so its log is that provider's. */
const OLLAMA_PROVIDER = "ollama-11434";

/** A line under each settings group; a group the machine adds later just goes without one. */
const GROUP_NOTES: Record<string, string> = {
  "Memory / KV cache": "How much context each request gets and how much VRAM the KV cache takes. These matter most for coding agents.",
  "Concurrency / scheduling": "How many requests and models the server handles at once, and how long models stay loaded.",
  Hardware: "Which GPUs and compute library the server runs on.",
  Server: "Where the server keeps models, and how much it logs.",
};

/** Client-side mirror of the machine's warnings so edits warn before saving. */
function warningsFor(config: OllamaConfig): string[] {
  const warnings: string[] = [];
  const context = Number(config.context_length) || 0;
  if (context < 8192) warnings.push("Context below 8K is likely too small for coding agents.");
  else if (context < 16384) warnings.push("Some coding agents may exhaust this context rapidly.");
  if (Number(config.num_parallel) > 1) warnings.push("Parallel requests increase KV cache memory requirements.");
  if (config.kv_cache_type !== "f16" && !config.flash_attention) warnings.push("A quantized KV cache requires Flash Attention; enable it.");
  return warnings;
}

function displayValue(setting: OllamaSetting, value: Value | undefined): string {
  if (setting.kind === "bool") return value ? "on" : "off";
  if (value === "" || value == null) return "empty";
  return setting.kind === "int" ? formatNumber(Number(value)) : String(value);
}

/** Variable names set in the extra environment text, which override any setting that maps to the same name. */
function extraNames(text: string): Set<string> {
  const names = new Set<string>();
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (line && !line.startsWith("#") && line.includes("=")) names.add(line.slice(0, line.indexOf("=")).trim());
  }
  return names;
}

/** The Ollama installed on a machine: start, stop, restart, the OLLAMA_* settings agent-machine starts it with, and its log. */
export function MachineOllama({ machine }: { machine: FleetMachine }) {
  if (!machine.connected) {
    return <p className="empty">{machine.name} isn't connected. Start agent-machine there (agent-machine start) to manage its Ollama.</p>;
  }
  if (!machine.servers.enabled) {
    return (
      <div className="callout">
        <p>
          {machine.name} doesn't let the server manage its model servers. Run <code>agent-machine servers on</code> there to start, stop and
          tune its Ollama from here.
        </p>
      </div>
    );
  }
  return <OllamaControls machine={machine} />;
}

function OllamaControls({ machine }: { machine: FleetMachine }) {
  const settings = useQuery({ queryKey: ["ollama-settings", machine.id], queryFn: () => api.ollamaSettings(machine.id), refetchInterval: 5000 });
  const client = useQueryClient();
  const notify = useToast();
  const [config, setConfig] = useState<OllamaConfig | null>(null);
  const [confirm, setConfirm] = useState<Action | "save-restart" | null>(null);
  const [tab, setTab] = useTabParam(TABS);
  const tabs = useId();
  const initialised = useRef(false);

  // Seed the form once; polling updates status without clobbering edits in progress.
  useEffect(() => {
    if (!initialised.current && settings.data) {
      setConfig(settings.data.config);
      initialised.current = true;
    }
  }, [settings.data]);

  const save = useMutation({
    mutationFn: () => api.saveOllamaSettings(machine.id, config as OllamaConfig),
    onSuccess: (info) => {
      setConfig(info.config);
      client.setQueryData(["ollama-settings", machine.id], info);
      notify("Ollama settings saved.");
    },
    onError: (error: Error) => notify(error.message),
  });

  const act = useMutation({
    mutationFn: (action: Action) => api.machineServer(machine.id, "ollama", action),
    onSuccess: (_, action) => {
      client.invalidateQueries({ queryKey: ["ollama-settings", machine.id] });
      client.invalidateQueries({ queryKey: ["ollama-log", machine.id] });
      client.invalidateQueries({ queryKey: ["machines"] });
      client.invalidateQueries({ queryKey: ["status"] });
      notify(`Ollama ${action === "stop" ? "stopped" : action === "restart" ? "restarted" : "started"} on ${machine.name}.`);
    },
    onError: (error: Error) => notify(error.message),
    onSettled: () => setConfirm(null),
  });

  const saveAndRestart = async () => {
    try {
      await save.mutateAsync();
    } catch {
      setConfirm(null); // save's onError has already reported it
      return;
    }
    act.mutate("restart");
  };

  if (settings.isPending) return <p className="empty">Asking {machine.name} for its Ollama settings…</p>;
  if (settings.isError) return <p className="bad">{(settings.error as Error).message}</p>;

  const info = settings.data as OllamaSettings;
  const status: OllamaServerState = info.status ?? machine.servers.ollama ?? { found: false, running: false, managed: false, version: null, bind: null };
  const owner = machine.you.launch;
  const busy = act.isPending;
  const changed = (key: string) => config != null && config[key] !== info.config[key];
  const changedGroups = info.schema.groups.filter((group) => info.schema.settings.some((setting) => setting.group === group && changed(setting.key)));
  const envChanged = changed("extra_env");
  const dirty = changedGroups.length > 0 || envChanged;
  const warnings = config ? warningsFor(config) : info.warnings;
  const update = (key: string, value: Value) => setConfig((current) => (current ? { ...current, [key]: value } : current));
  const canRestart = owner && status.managed && status.running;

  return (
    <>
      <StatusCard
        machine={machine}
        status={status}
        appliedMatchesSaved={info.applied_matches_saved}
        busy={busy}
        owner={owner}
        onStart={() => act.mutate("start")}
        onRestart={() => setConfirm("restart")}
        onStop={() => setConfirm("stop")}
      />

      <div className="tabbed">
        <TabBar
          label="Server sections"
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
          {config && <SettingsTab info={info} config={config} warnings={warnings} changed={changed} onChange={update} />}
        </TabPanel>
        <TabPanel idPrefix={tabs} id="environment" active={tab === "environment"}>
          {config && <EnvironmentTab info={info} config={config} dirty={dirty} onChange={update} />}
        </TabPanel>
        <TabPanel idPrefix={tabs} id="log" active={tab === "log"}>
          <LogPanel
            queryKey={["ollama-log", machine.id]}
            load={() => fetchProviderLog({ machine: machine.id, provider: OLLAMA_PROVIDER })}
            tone={ollamaTone}
            views={OLLAMA_LOG_VIEWS}
            defaultView="quiet"
            empty="No log yet. It covers an Ollama agent-machine started; one started another way writes its own log."
            footer={<>From <code>{info.log_path}</code> on {machine.name}</>}
          />
        </TabPanel>
      </div>

      {tab !== "log" && config && (
        <div className="save-bar">
          <p className={dirty ? "warn" : "subtle"}>
            {!owner
              ? `Only ${machine.owner ?? "its owner"} can change ${machine.name}'s Ollama.`
              : dirty
                ? `Unsaved changes in ${[...changedGroups, ...(envChanged ? ["Environment"] : [])].join(", ")}.`
                : info.applied_matches_saved === false
                  ? "Saved. Restart Ollama to apply the saved settings."
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
        title="Stop Ollama?"
        confirmLabel={act.isPending ? "Stopping…" : "Stop server"}
        busy={act.isPending}
        onConfirm={() => act.mutate("stop")}
        onCancel={() => setConfirm(null)}
      >
        <p>This stops the Ollama agent-machine started on {machine.name}. Running agents and loaded models will be interrupted.</p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirm === "restart" || confirm === "save-restart"}
        title={confirm === "save-restart" ? "Save and restart Ollama?" : "Restart Ollama?"}
        confirmLabel={save.isPending ? "Saving…" : act.isPending ? "Restarting…" : "Restart server"}
        busy={save.isPending || act.isPending}
        onConfirm={() => (confirm === "save-restart" ? void saveAndRestart() : act.mutate("restart"))}
        onCancel={() => setConfirm(null)}
      >
        <p>
          {confirm === "save-restart" ? "This saves your changes, then stops" : "This stops"} the running server and starts it again with
          the saved settings. In-flight requests will be interrupted.
        </p>
      </ConfirmDialog>
    </>
  );
}

function StatusCard({
  machine,
  status,
  appliedMatchesSaved,
  busy,
  owner,
  onStart,
  onRestart,
  onStop,
}: {
  machine: FleetMachine;
  status: OllamaServerState;
  appliedMatchesSaved: boolean | null;
  busy: boolean;
  owner: boolean;
  onStart: () => void;
  onRestart: () => void;
  onStop: () => void;
}) {
  const state = busy ? "busy" : status.running ? "running" : "stopped";
  // agent-machine only stops the Ollama it started itself; one from a tray app or service is left alone.
  const stoppable = status.running && status.managed;
  return (
    <section className={`status-card is-${state}`} aria-label="Server status">
      <div className="status-card-head">
        <h2>
          {status.running && status.version ? `Ollama ${status.version}` : "Ollama server"}
          <span className={`pill ${state === "running" ? "good" : state === "busy" ? "live" : ""}`}>
            {state === "busy" ? "Working…" : state === "running" ? "Running" : "Stopped"}
          </span>
        </h2>
        <div className="row">
          <button type="button" className="primary" disabled={!owner || busy || status.running || !status.found} onClick={onStart}>
            Start
          </button>
          <button type="button" disabled={!owner || busy || !status.found || (status.running && !status.managed)} onClick={onRestart}>
            Restart
          </button>
          <button type="button" className="danger" disabled={!owner || busy || !stoppable} onClick={onStop}>
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
          <dd className="mono">{status.bind ? `${status.bind}:11434` : status.running ? "port 11434" : "–"}</dd>
        </div>
        <div>
          <dt>Started by</dt>
          <dd>
            {!status.running
              ? "–"
              : status.service === "systemd"
                ? status.service_allowed
                  ? "Its systemd service, through agent-machine"
                  : "Its systemd service"
                : status.managed
                  ? "agent-machine"
                  : "Outside agent-machine"}
          </dd>
        </div>
        <div>
          <dt>Installed</dt>
          <dd>{status.found ? "Yes" : "Not found on PATH"}</dd>
        </div>
      </dl>
      {!status.found && (
        <div className="callout">
          <p>
            Ollama isn't installed on {machine.name}, or isn't on its PATH. Install it from{" "}
            <a href="https://ollama.com/download" target="_blank" rel="noreferrer">ollama.com/download</a>.
          </p>
        </div>
      )}
      {status.service === "systemd" && !status.service_allowed && <ServiceSetup machine={machine.name} />}
      {status.running && !status.managed && status.service !== "systemd" && <OutsideOllama machine={machine} />}
      {status.running && appliedMatchesSaved === false && (
        <div className="callout">
          <p className="warn">The running server was started with different settings. Restart to apply the saved configuration.</p>
        </div>
      )}
    </section>
  );
}

function SettingsTab({
  info,
  config,
  warnings,
  changed,
  onChange,
}: {
  info: OllamaSettings;
  config: OllamaConfig;
  warnings: string[];
  changed: (key: string) => boolean;
  onChange: (key: string, value: Value) => void;
}) {
  const { groups, settings } = info.schema;
  const [group, setGroup] = useState(groups[0]);
  const ids = useId();
  return (
    <div className="tab-stack">
      {warnings.length > 0 && (
        <div className="callout">
          {warnings.map((warning) => (
            <p key={warning} className="warn">
              {warning}
            </p>
          ))}
        </div>
      )}
      <div className="settings-layout">
        <TabBar
          vertical
          label="Setting groups"
          idPrefix={ids}
          active={group}
          onSelect={setGroup}
          tabs={groups.map((name) => ({
            id: name,
            label: name,
            badge: settings.some((setting) => setting.group === name && changed(setting.key)) ? <UnsavedDot /> : undefined,
          }))}
        />
        {groups.map((name) => (
          <TabPanel key={name} idPrefix={ids} id={name} active={name === group} className="panel">
            <div className="panel-head">
              <h2>{name}</h2>
              {GROUP_NOTES[name] && <p className="subtle">{GROUP_NOTES[name]}</p>}
            </div>
            <div className="setting-grid">
              {settings
                .filter((setting) => setting.group === name)
                .map((setting) => (
                  <SettingField key={setting.key} setting={setting} value={config[setting.key]} unsaved={changed(setting.key)} onChange={onChange} />
                ))}
            </div>
          </TabPanel>
        ))}
      </div>
    </div>
  );
}

function SettingField({
  setting,
  value,
  unsaved,
  onChange,
}: {
  setting: OllamaSetting;
  value: Value | undefined;
  unsaved: boolean;
  onChange: (key: string, value: Value) => void;
}) {
  const id = useId();
  const listId = useId();
  const isDefault = String(value ?? "") === String(setting.default);
  return (
    <div className="setting">
      <div className="setting-head">
        {setting.kind === "bool" ? (
          <label className="check">
            <input id={id} type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(setting.key, event.target.checked)} />
            {setting.label}
          </label>
        ) : (
          <label htmlFor={id}>{setting.label}</label>
        )}
        {unsaved && <UnsavedDot />}
        {!isDefault && (
          <button type="button" className="link" title={`Reset to ${displayValue(setting, setting.default)}`} onClick={() => onChange(setting.key, setting.default)}>
            Reset
          </button>
        )}
      </div>
      {setting.kind === "int" ? (
        <input
          id={id}
          type="number"
          min={setting.minimum}
          max={setting.maximum}
          value={Number(value ?? 0)}
          onChange={(event) => onChange(setting.key, Number(event.target.value))}
        />
      ) : setting.kind === "choice" && !setting.editable ? (
        <select id={id} value={String(value ?? "")} onChange={(event) => onChange(setting.key, event.target.value)}>
          {setting.choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice || "(default)"}
            </option>
          ))}
        </select>
      ) : setting.kind !== "bool" ? (
        <>
          <input
            id={id}
            list={setting.choices.length ? listId : undefined}
            value={String(value ?? "")}
            onChange={(event) => onChange(setting.key, event.target.value)}
          />
          {setting.choices.length > 0 && (
            <datalist id={listId}>
              {setting.choices.map((choice) => (
                <option key={choice} value={choice} />
              ))}
            </datalist>
          )}
        </>
      ) : null}
      {setting.help && <small>{setting.help}</small>}
      <small className="setting-env">
        {setting.env.join(" · ")}
        {!isDefault && <span> · default {displayValue(setting, setting.default)}</span>}
      </small>
    </div>
  );
}

function EnvironmentTab({
  info,
  config,
  dirty,
  onChange,
}: {
  info: OllamaSettings;
  config: OllamaConfig;
  dirty: boolean;
  onChange: (key: string, value: Value) => void;
}) {
  const sources = new Map<string, string>();
  for (const setting of info.schema.settings) for (const name of setting.env) sources.set(name, setting.label);
  const extra = extraNames(String(info.config.extra_env ?? ""));
  const variables = Object.entries(info.env);
  return (
    <div className="tab-stack">
      <section className="panel">
        <div className="panel-head">
          <h2>Extra variables</h2>
          <p className="subtle">
            Other <code>OLLAMA_*</code> variables the settings don't cover. One <code>KEY=VALUE</code> per line, applied on top of the
            settings when the server starts. A blank value unsets the variable, and <code>#</code> starts a comment. The machine only
            accepts names starting with <code>OLLAMA_</code>.
          </p>
        </div>
        <textarea
          rows={6}
          aria-label="Extra environment variables"
          value={String(config.extra_env ?? "")}
          placeholder={"# OLLAMA_EXPERIMENT=1\n# OLLAMA_INTEL_GPU=1"}
          onChange={(event) => onChange("extra_env", event.target.value)}
        />
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>What Ollama starts with</h2>
          <p className="subtle">
            Variables the saved settings set or clear when agent-machine runs <code>ollama serve</code>. Everything else in agent-machine's
            environment passes through unchanged.{dirty && " Save to see your changes here."}
          </p>
        </div>
        {variables.length ? (
          <div className="table-scroll">
            <table className="table compact">
              <thead>
                <tr>
                  <th scope="col">Variable</th>
                  <th scope="col">Value</th>
                  <th scope="col">Set by</th>
                </tr>
              </thead>
              <tbody>
                {variables.map(([name, value]) => (
                  <tr key={name}>
                    <td className="mono">{name}</td>
                    <td className="mono">{value ?? <span className="subtle">unset</span>}</td>
                    <td>{extra.has(name) || !sources.has(name) ? "Extra variables" : sources.get(name)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="empty">Nothing set.</p>
        )}
      </section>
    </div>
  );
}

/** Ollama's own log lines, plus the GIN line it writes for every HTTP request (mostly agent-machine and the server polling it). */
function ollamaTone(line: string): LogTone {
  if (/level=(ERROR|FATAL)|panic:|^Error:/i.test(line)) return "bad";
  if (/level=WARN/i.test(line)) return "warn";
  if (line.startsWith("[GIN]")) return "muted";
  return "";
}

const OLLAMA_LOG_VIEWS = [ALL_LINES, quietView("Hide HTTP requests"), PROBLEMS];

/** Ollama there is a systemd service: one command on the machine lets agent-machine restart it with these settings. */
/** How to quit an Ollama agent-machine didn't start (usually its tray app), so Start here runs one it manages. */
function OutsideOllama({ machine }: { machine: FleetMachine }) {
  const copy = useClipboard();
  const command =
    machine.os === "windows"
      ? 'Stop-Process -Name "ollama app", ollama -Force -ErrorAction SilentlyContinue'
      : machine.os === "darwin"
        ? "osascript -e 'quit app \"Ollama\"'"
        : "pkill -x ollama";
  const shell = machine.os === "windows" ? "PowerShell" : "a terminal";
  return (
    <div className="callout">
      <p>
        Ollama's own app started this Ollama, so it can't be stopped or restarted from here, and these settings don't apply to it. To
        hand it over, quit it by running this in {shell} on {machine.name}, then press Start here:
      </p>
      <CodeLine text={command} onCopy={() => copy(command, "Command copied")} />
      <p className="subtle">
        {machine.os === "windows" ? (
          <>
            The app starts again when you next sign in to Windows. To stop that, turn Ollama off in Settings › Apps › Startup.
          </>
        ) : machine.os === "darwin" ? (
          <>The app starts again when you next sign in. To stop that, remove Ollama from System Settings › General › Login Items.</>
        ) : (
          <>If something starts it again when you sign in, turn that off too.</>
        )}
      </p>
    </div>
  );
}

function ServiceSetup({ machine }: { machine: string }) {
  const copy = useClipboard();
  const command = "sudo ~/.local/bin/agent-machine servers service";
  return (
    <div className="callout">
      <p>
        Ollama on {machine} is the systemd service <code>ollama.service</code>, which belongs to root. To start, stop and restart it from
        here, and apply these settings when it restarts, run this there once:
      </p>
      <CodeLine text={command} onCopy={() => copy(command, "Command copied")} />
      <p className="subtle">
        It lets only your user run <code>systemctl start|stop|restart ollama</code> without a password, and has the service read its
        settings from a file agent-machine writes. Undo it with <code>sudo ~/.local/bin/agent-machine servers service off</code>.
      </p>
    </div>
  );
}
