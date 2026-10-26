import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, freeBackendName, type ContainerEngine, type ContainerHost, type ContainerPhase, type ContainerSpec, type ContainerStatus, type ContainersOverview } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ALL_LINES, LogPanel, PROBLEMS, type LogTone } from "../components/LogPanel";
import { useClipboard, useToast } from "../hooks";
import { PairPanel } from "./Terminals";

const POLL_MS = 3000;

const PHASES: Record<ContainerPhase, { label: string; tone: "good" | "live" | "bad" | "" ; detail: string }> = {
  not_created: { label: "Not created", tone: "", detail: "Start creates the container." },
  pulling: { label: "Pulling image", tone: "live", detail: "Downloading the engine's image." },
  starting: { label: "Starting", tone: "live", detail: "Creating and starting the container." },
  loading: { label: "Loading model", tone: "live", detail: "The engine is loading its model; the first start also downloads it." },
  ready: { label: "Ready", tone: "good", detail: "Serving requests." },
  unhealthy: { label: "Unhealthy", tone: "bad", detail: "Running, but failing its health check. See the log." },
  stopping: { label: "Stopping", tone: "live", detail: "Unloading the model and stopping." },
  stopped: { label: "Stopped", tone: "", detail: "Start it, or switch to it to free the GPU first." },
  failed: { label: "Failed", tone: "bad", detail: "The last action failed. See below and in the log." },
};

const BUSY: ContainerPhase[] = ["pulling", "starting", "stopping"];

function logTone(line: string): LogTone {
  if (/\b(error|failed|fatal|panic|out of memory)\b/i.test(line)) return "bad";
  if (/\bwarn(ing)?\b/i.test(line)) return "warn";
  return "";
}

/**
 * The containers one machine runs for the server: their state, starting, stopping and switching
 * the GPU between them, their logs, and adding one. The machine runs them (agent-machine
 * containers on); the server keeps their settings and keys.
 */
export function MachineContainers({ machine }: { machine: string }) {
  const remote = useQuery({ queryKey: ["remote"], queryFn: api.remote });
  const trusted = remote.data?.trusted === true;
  const overview = useQuery({ queryKey: ["containers"], queryFn: () => api.containers(), refetchInterval: POLL_MS, enabled: trusted });
  const data = overview.data;
  const host = data?.machines.find((item) => item.id === machine);
  const mine = data?.containers.filter((item) => item.machine === machine) ?? [];

  if (remote.data && !trusted) return <PairPanel />;
  if (overview.isError) return <p className="bad">{overview.error.message}</p>;
  if (!data) return <p className="empty">Reading the containers…</p>;
  return (
    <div className="tab-stack">
      {host && <HostState host={host} />}
      <section className="section" aria-labelledby="containers-heading">
        <h2 id="containers-heading">Containers</h2>
        {mine.length ? (
          <ul className="container-list">
            {mine.map((item) => (
              <ContainerCard key={item.name} item={item} engine={host?.engines.find((engine) => engine.id === item.engine)} />
            ))}
          </ul>
        ) : (
          <p className="subtle">None on this machine yet. Add one below; it becomes a provider agents can use.</p>
        )}
        {mine.length > 0 && (
          <p className="subtle">
            Pick a container as the provider on <Link to="/agents">Start an agent</Link>. <code>agent … --backend {"<name>"}</code> starts
            it first if it isn't running; add <code>--switch</code> to take the GPU from another container.
          </p>
        )}
      </section>
      {host && <AddContainer overview={data} host={host} />}
      {host && <StorageNote engines={host.engines} />}
    </div>
  );
}

/** Whether the machine runs containers for the server, and whether its Docker or Podman answers. */
function HostState({ host }: { host: ContainerHost }) {
  if (!host.enabled) {
    return (
      <div className="callout">
        <p>
          <strong>{host.name} doesn't run containers for the server.</strong> With Docker or Podman installed there, run{" "}
          <code>agent-machine containers on</code> on it. It uses whichever is installed and answering.
        </p>
      </div>
    );
  }
  const tone = !host.connected ? "idle" : host.online ? "good" : "bad";
  return (
    <p className="row wrap">
      <span className={`state-dot ${tone}`} aria-hidden="true" />
      <span className="subtle">
        {!host.connected
          ? "Away: agent-machine isn't running there"
          : !host.found
            ? `${host.cli} isn't installed there`
            : host.online
              ? `Runs containers with ${host.cli} ${host.version ?? ""}: ${host.engines.map((engine) => engine.label).join(", ")}`
              : `${host.cli} isn't answering there: start Docker Desktop or the service`}
      </span>
      {host.connected && host.found && !host.online && host.error && <pre className="code wrap">{host.error}</pre>}
    </p>
  );
}

function summary(spec: ContainerSpec) {
  const settings = Object.entries(spec.env).map(([key, value]) => `${key}=${value}`);
  return settings.length ? settings.join(" · ") : "Engine defaults";
}

function ContainerCard({ item, engine }: { item: ContainerStatus; engine?: ContainerEngine }) {
  const client = useQueryClient();
  const notify = useToast();
  const copy = useClipboard();
  const [showLog, setShowLog] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const phase = PHASES[item.phase] ?? PHASES.stopped;
  const busy = BUSY.includes(item.phase) || item.job?.state === "running";
  const running = Boolean(item.state?.running);
  const refresh = () => client.invalidateQueries({ queryKey: ["containers"] });
  const act = useMutation({
    mutationFn: ({ action, switchGpu }: { action: "start" | "stop" | "restart"; switchGpu?: boolean }) =>
      api.containerAction(item.name, action, { switch: switchGpu }),
    onSuccess: refresh,
    onError: (error) => notify(error.message),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteContainer(item.name),
    onSuccess: () => {
      notify(`${item.name} deleted. Its models stay in the shared storage.`);
      setConfirmDelete(false);
      client.invalidateQueries();
    },
  });
  const failed = item.job?.state === "failed";
  const away = !item.machine_online;

  return (
    <li className={`container-card tone-${phase.tone || "idle"}`}>
      <div className="container-card-head">
        <div>
          <h3>
            {item.name}
            {item.default && <span className="pill accent">Default</span>}
          </h3>
          <p className="subtle">
            {item.engine_label} container on <strong>{item.machine_name ?? "a removed machine"}</strong> · <span className="mono">{item.url}</span>
          </p>
        </div>
        <span className={`pill ${phase.tone}`}>{phase.label}</span>
      </div>
      <p className="subtle">{phase.detail}</p>
      {item.problem && <p className="bad">{item.problem}</p>}
      <dl className="provider-facts">
        <div>
          <dt>Image</dt>
          <dd className="mono">{item.spec.image}</dd>
        </div>
        <div>
          <dt>Settings</dt>
          <dd className="mono">{summary(item.spec)}</dd>
        </div>
        <div>
          <dt>GPUs</dt>
          <dd>{item.spec.gpus === "all" ? "All" : item.spec.gpus === "none" ? "None (CPU only)" : `Card ${item.spec.gpus}`}</dd>
        </div>
        <div>
          <dt>Model storage</dt>
          <dd className="mono">
            {item.volume} → {item.data_path ?? engine?.data_path}
          </dd>
        </div>
      </dl>
      {item.outdated && (
        <p className="warn">
          Its settings changed. {running ? "Restart it to apply them" : "They apply when it next starts"}: the container is replaced, the
          models stay.
        </p>
      )}
      {item.job && (item.job.state === "running" || failed) && (
        <pre className={`code wrap container-job ${failed ? "bad" : ""}`}>{item.job.lines.slice(-6).join("\n") || "…"}</pre>
      )}
      <div className="row wrap">
        {!running && !busy && !away && (
          item.conflicts.length ? (
            <button
              type="button"
              className="primary"
              disabled={act.isPending}
              title={`Stops ${item.conflicts.join(", ")} first`}
              onClick={() => act.mutate({ action: "start", switchGpu: true })}
            >
              Switch to this
            </button>
          ) : (
            <button type="button" className="primary" disabled={act.isPending} onClick={() => act.mutate({ action: "start" })}>
              Start
            </button>
          )
        )}
        {running && !busy && !away && (
          <>
            <button type="button" disabled={act.isPending} onClick={() => act.mutate({ action: item.outdated ? "restart" : "stop" })}>
              {item.outdated ? "Restart to apply" : "Stop"}
            </button>
            {!item.outdated && (
              <button type="button" disabled={act.isPending} onClick={() => act.mutate({ action: "restart" })}>
                Restart
              </button>
            )}
          </>
        )}
        <button type="button" aria-expanded={showLog} disabled={away} onClick={() => setShowLog(!showLog)}>
          {showLog ? "Hide log" : "Log"}
        </button>
        <button type="button" aria-expanded={editing} disabled={away || !engine} onClick={() => setEditing(!editing)}>
          Edit
        </button>
        <button type="button" className="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
          Delete…
        </button>
      </div>
      {!running && !busy && item.conflicts.length > 0 && (
        <p className="subtle">
          {item.conflicts.join(", ")} {item.conflicts.length === 1 ? "is" : "are"} using the GPU; switching stops{" "}
          {item.conflicts.length === 1 ? "it" : "them"} first.
        </p>
      )}
      {editing && engine && (
        <ContainerForm
          engine={engine}
          initial={item.spec}
          submitLabel="Save settings"
          onSubmit={async (spec) => {
            await api.updateContainer(item.name, spec);
            notify(running ? `Saved. Restart ${item.name} to apply.` : "Saved. They apply when it next starts.");
            setEditing(false);
            refresh();
          }}
        />
      )}
      {showLog && (
        <LogPanel
          queryKey={["container-log", item.name]}
          load={() => api.containerLog(item.name)}
          tone={logTone}
          views={[ALL_LINES, PROBLEMS]}
          empty={item.state ? "Nothing logged yet." : "No container yet: start it to see its log."}
          footer={`docker logs for ${item.container}, the API key blanked out.`}
        />
      )}
      {item.command && (
        <details className="container-command">
          <summary>The command its machine runs</summary>
          <CodeLine text={item.command} onCopy={() => copy(item.command!, "Command copied")} />
        </details>
      )}
      <ConfirmDialog
        open={confirmDelete}
        title={`Delete ${item.name}?`}
        confirmLabel={remove.isPending ? "Deleting…" : "Delete container"}
        busy={remove.isPending}
        error={remove.isError ? remove.error.message : null}
        onConfirm={() => remove.mutate()}
        onCancel={() => setConfirmDelete(false)}
      >
        <p>
          This removes the container from {item.machine_name ?? "its machine"} and the {item.name} provider. The models stay in{" "}
          <code>{item.volume}</code> there for your other containers.
        </p>
      </ConfirmDialog>
    </li>
  );
}

/** Image, port, GPUs and the engine's own settings, for a new container or an existing one. */
function ContainerForm({
  engine,
  initial,
  submitLabel,
  onSubmit,
  name,
}: {
  engine: ContainerEngine;
  initial?: ContainerSpec;
  submitLabel: string;
  onSubmit: (spec: Partial<ContainerSpec>) => Promise<void>;
  name?: ReactNode;
}) {
  const [image, setImage] = useState(initial?.image ?? engine.image);
  const [port, setPort] = useState(initial ? String(initial.port) : "");
  const [gpus, setGpus] = useState(initial?.gpus ?? "all");
  const [volume, setVolume] = useState(initial?.volume ?? "");
  const [env, setEnv] = useState<Record<string, string>>(initial?.env ?? {});
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await onSubmit({ image: image.trim(), gpus: gpus.trim() || "none", env, volume: volume.trim() || undefined, ...(port.trim() ? { port: Number(port) } : {}) });
    } catch (failure) {
      setError(failure instanceof ApiError || failure instanceof Error ? failure.message : String(failure));
    } finally {
      setSaving(false);
    }
  };
  const listId = `images-${engine.id}`;
  return (
    <form className="container-form" onSubmit={submit}>
      <div className="form-grid two">
        {name}
        <label className="field">
          Image
          <input list={listId} value={image} onChange={(event) => setImage(event.target.value)} required spellCheck={false} />
          <datalist id={listId}>
            {engine.images.map((value) => (
              <option key={value} value={value} />
            ))}
          </datalist>
        </label>
        <label className="field">
          Port on the machine
          <input value={port} onChange={(event) => setPort(event.target.value)} inputMode="numeric" placeholder={`Free one from ${engine.port}`} />
        </label>
        <label className="field">
          GPUs
          <input list="gpu-choices" value={gpus} onChange={(event) => setGpus(event.target.value)} placeholder="all, none, 0 or 0,1" />
          <datalist id="gpu-choices">
            <option value="all" />
            <option value="none" />
            <option value="0" />
            <option value="1" />
          </datalist>
        </label>
        <label className="field">
          <span className="field-label">
            Model storage <span className="optional">optional</span>
          </span>
          <input value={volume} onChange={(event) => setVolume(event.target.value)} placeholder={`Shared: ${engine.volume}`} spellCheck={false} />
          <small>A volume name, or a folder on the machine such as D:\models.</small>
        </label>
        {engine.options.map((option) => (
          <label key={option.env} className="field">
            {option.label} <span className="mono subtle">{option.env}</span>
            <input
              list={`${engine.id}-${option.env}`}
              value={env[option.env] ?? ""}
              placeholder={option.hint ? `Default: ${option.hint}` : "Default"}
              onChange={(event) => setEnv((current) => ({ ...current, [option.env]: event.target.value }))}
              spellCheck={false}
            />
            <datalist id={`${engine.id}-${option.env}`}>
              {option.choices.map((choice) => (
                <option key={choice} value={choice} />
              ))}
            </datalist>
          </label>
        ))}
      </div>
      <div className="row wrap">
        <button type="submit" className="primary" disabled={saving}>
          {saving ? "Saving…" : submitLabel}
        </button>
        {error && <span className="bad">{error}</span>}
      </div>
    </form>
  );
}

function AddContainer({ overview, host: machine }: { overview: ContainersOverview; host: ContainerHost }) {
  const client = useQueryClient();
  const notify = useToast();
  // Containers can be added on your own machine, connected, with containers on.
  const host = machine.enabled && machine.connected && machine.you.owner && machine.engines.length ? machine : undefined;
  const [engineId, setEngineId] = useState("strata");
  const engines = host?.engines ?? [];
  const engine = engines.find((item) => item.id === engineId) ?? engines[0];
  const taken = useMemo(() => overview.containers.map((item) => item.name), [overview.containers]);
  const [name, setName] = useState("");
  const [startNow, setStartNow] = useState(true);
  if (!host || !engine) {
    return machine.enabled ? (
      <p className="subtle">
        To add a container, the machine has to be yours and connected, with <code>agent-machine containers on</code>.
      </p>
    ) : null;
  }
  const suggested = freeBackendName(engine.id, taken);
  return (
    <section className="panel" aria-labelledby="add-container-heading">
      <div className="panel-head">
        <h2 id="add-container-heading">Add a container</h2>
        <p className="subtle">{engine.summary}</p>
      </div>
      <div className="row wrap" role="radiogroup" aria-label="Engine">
        {engines.map((item) => (
          <label key={item.id} className="check">
            <input type="radio" name="engine" checked={item.id === engine.id} onChange={() => setEngineId(item.id)} />
            {item.label}
          </label>
        ))}
      </div>
      <ContainerForm
        key={`${host.id}-${engine.id}`}
        engine={engine}
        submitLabel={startNow ? "Add and start" : "Add container"}
        name={
          <label className="field">
            Name, also the provider's name
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder={suggested} pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" />
          </label>
        }
        onSubmit={async (spec) => {
          const chosen = name.trim() || suggested;
          await api.createContainer({ name: chosen, machine: host.id, engine: engine.id, ...spec });
          if (startNow) {
            try {
              await api.containerAction(chosen, "start");
            } catch (failure) {
              notify(failure instanceof Error ? `${chosen} added. ${failure.message}` : `${chosen} added.`);
            }
          }
          setName("");
          client.invalidateQueries();
        }}
      />
      <label className="check">
        <input type="checkbox" checked={startNow} onChange={(event) => setStartNow(event.target.checked)} />
        Start it right away (switch to it yourself if another container has the GPU)
      </label>
    </section>
  );
}

function StorageNote({ engines: list }: { engines: ContainerEngine[] }) {
  const engines = new Map<string, ContainerEngine>(list.map((engine) => [engine.id, engine]));
  if (!engines.size) return null;
  return (
    <section className="section" aria-labelledby="storage-heading">
      <h2 id="storage-heading">Shared model storage</h2>
      <ul className="storage-list">
        {[...engines.values()].map((engine) => (
          <li key={engine.id}>
            <strong>{engine.label}</strong>: <code>{engine.volume}</code> mounted at <code>{engine.data_path}</code> in every{" "}
            {engine.label} container on a machine.
            {engine.id === "strata" && " Each container keeps its own settings, tuning and key there under its own PRESET name."}
          </li>
        ))}
      </ul>
      <p className="subtle">
        Give a container its own storage with Model storage: a volume name, or a folder on its machine such as{" "}
        <code>D:\models\strata</code> (named volumes are faster on Docker Desktop). Deleting a container never deletes its
        models.
      </p>
    </section>
  );
}
