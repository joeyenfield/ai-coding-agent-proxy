import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchHost, type HostSample, type HostState } from "../api";
import { LineChart, type Sample } from "../components/LineChart";
import { formatBytes, formatNumber } from "../format";
import { useStatus } from "../hooks";
import { useTicker } from "../live";
import { OllamaOnlyNote } from "../components/OllamaOnlyNote";

const POLL_MS = 2000;
const WINDOW_SECONDS = 300;

interface History {
  cpu: Sample[];
  memory: Sample[];
  modelVram: Sample[];
  gpuUtil: Sample[][];
  gpuMemory: Sample[][];
}

const emptyHistory = (): History => ({ cpu: [], memory: [], modelVram: [], gpuUtil: [], gpuMemory: [] });
const GB = 1024 ** 3;
const percent = (value: number) => `${Math.round(value)}%`;
const gigabytes = (value: number) => `${value.toFixed(1)} GB`;
const gigabytesAxis = (value: number) => (value === 0 ? "0 GB" : `${value.toFixed(value >= 10 ? 0 : 1)} GB`);

/** Samples live only in the open panel, so collapsing a host discards its history. */
function useHostHistory(sample: HostSample | undefined) {
  const history = useRef(emptyHistory());
  const [, setVersion] = useState(0);
  const lastSample = useRef(0);
  useEffect(() => {
    if (!sample || sample.sampled_at === lastSample.current) return;
    lastSample.current = sample.sampled_at;
    const cutoff = sample.sampled_at - WINDOW_SECONDS;
    const push = (series: Sample[], v: number | null) => {
      series.push({ t: sample.sampled_at, v });
      while (series.length && series[0].t < cutoff) series.shift();
    };
    const entry = history.current;
    const machine = sample.machine.data;
    push(entry.cpu, machine ? machine.cpu.percent : null);
    push(entry.memory, machine ? machine.memory.used / GB : null);
    push(entry.modelVram, sample.online ? sample.models.reduce((total, model) => total + model.size_vram, 0) / GB : null);
    machine?.gpus.forEach((gpu, index) => {
      entry.gpuUtil[index] ??= [];
      entry.gpuMemory[index] ??= [];
      push(entry.gpuUtil[index], gpu.utilization);
      push(entry.gpuMemory[index], gpu.memory_used != null ? gpu.memory_used / GB : null);
    });
    setVersion((value) => value + 1);
  }, [sample]);
  return history.current;
}

export function HostsPage() {
  // The host list comes from the status poll the app already runs; no backend is contacted until opened.
  const status = useStatus();
  const backends = Object.entries(status.data?.backends ?? {}).filter(([, backend]) => backend.type === "ollama");
  return (
    <div className="page wide">
      <header className="page-head">
        <h1>Ollama machines</h1>
        <p className="lede">
          What each Ollama machine has loaded and how hard it's working. Open a host to sample it every{" "}
          {POLL_MS / 1000} seconds. Closed hosts aren't contacted, and nothing is saved.
        </p>
      </header>
      <OllamaOnlyNote />

      {status.isError && <p className="bad">{status.error.message}</p>}
      {status.isPending && <p className="empty">Connecting to the proxy…</p>}
      <div className="host-list">
        {backends.map(([name, backend]) => (
          <HostPanel
            key={name}
            name={name}
            url={backend.url}
            isDefault={name === status.data?.default_backend}
            inFlight={status.data?.current_requests.filter((item) => item.backend === name).length ?? 0}
          />
        ))}
      </div>
    </div>
  );
}

function HostPanel({ name, url, isDefault, inFlight }: { name: string; url: string; isDefault: boolean; inFlight: number }) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();
  return (
    <section className={`host ${open ? "is-open" : ""}`} aria-label={name}>
      <h2 className="host-title">
        <button type="button" className="host-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
          <span className="host-chevron" aria-hidden="true" />
          <span className="host-name">{name}</span>
          <span className="host-url subtle">
            {url}
            {isDefault && ", default"}
          </span>
          {inFlight > 0 && <span className="pill live">{inFlight} in flight</span>}
        </button>
      </h2>
      <div id={bodyId} hidden={!open}>
        {open && <HostBody name={name} />}
      </div>
    </section>
  );
}

function HostBody({ name }: { name: string }) {
  // Mounted only while the panel is open, so polling stops as soon as it closes.
  const query = useQuery({ queryKey: ["host", name], queryFn: () => fetchHost(name), refetchInterval: POLL_MS, gcTime: 0 });
  const history = useHostHistory(query.data);
  useTicker(true, 1000);
  if (query.isPending) return <p className="host-body subtle">Contacting {name}…</p>;
  if (query.isError) return <p className="host-body bad">{query.error.message}</p>;
  return <HostDetails host={query.data} history={history} />;
}

function HostDetails({ host, history }: { host: HostState; history: History }) {
  const machine = host.machine.data;
  const vramTotal = machine?.gpus.reduce((total, gpu) => total + (gpu.memory_total ?? 0), 0) || null;
  return (
    <div className="host-body">
      <div className="host-head">
        <p className={host.online ? "subtle" : "bad"}>
          {host.online ? `Ollama ${host.version}` : `Offline: ${host.error}`}
          {machine && ` on ${machine.hostname}, ${machine.platform}`}
        </p>
        <dl className="host-traffic">
          <div>
            <dt>In flight</dt>
            <dd className={host.traffic.in_flight ? "warn" : undefined}>{host.traffic.in_flight}</dd>
          </div>
          <div>
            <dt>Recent speed</dt>
            <dd>{host.traffic.average_tps == null ? "–" : `${formatNumber(host.traffic.average_tps)} tok/s`}</dd>
          </div>
          <div>
            <dt>Loaded</dt>
            <dd>{host.models.length}</dd>
          </div>
        </dl>
      </div>

      <div className="metric-grid-layout">
        {machine && (
          <>
            <LineChart label="CPU" samples={history.cpu} max={100} format={percent} windowSeconds={WINDOW_SECONDS} detail={`${machine.cpu.count} cores`} />
            <LineChart
              label="Memory"
              samples={history.memory}
              max={machine.memory.total / GB}
              format={gigabytes}
              formatAxis={gigabytesAxis}
              windowSeconds={WINDOW_SECONDS}
              detail={`of ${formatBytes(machine.memory.total)}`}
            />
            {machine.gpus.map((gpu, index) => (
              <GpuCharts key={gpu.index} gpu={gpu} util={history.gpuUtil[index] ?? []} memory={history.gpuMemory[index] ?? []} multiple={machine.gpus.length > 1} />
            ))}
          </>
        )}
        <LineChart
          label="Ollama models in VRAM"
          samples={history.modelVram}
          max={Math.max(vramTotal ? vramTotal / GB : 0, ...history.modelVram.map((sample) => sample.v ?? 0), 1)}
          format={gigabytes}
          formatAxis={gigabytesAxis}
          windowSeconds={WINDOW_SECONDS}
          detail={vramTotal ? `of ${formatBytes(vramTotal)} on the GPU` : "from ollama ps"}
        />
      </div>
      {!machine && (
        <div className="callout">
          <p>{host.machine.error}</p>
          {host.machine.source === null && (
            <p>
              On that machine run <code>pip install -e ./proxy</code> and <code>agent-metrics</code>, then add{" "}
              <code>http://&lt;its address&gt;:8182</code> as the metrics URL in <Link to="/settings">Settings</Link>.
            </p>
          )}
        </div>
      )}
      {machine?.gpu_error && <p className="subtle">{machine.gpu_error}</p>}

      <RunningModels host={host} />
    </div>
  );
}

function GpuCharts({ gpu, util, memory, multiple }: { gpu: NonNullable<HostState["machine"]["data"]>["gpus"][number]; util: Sample[]; memory: Sample[]; multiple: boolean }) {
  const prefix = multiple ? `GPU ${gpu.index} ` : "GPU ";
  const facts = [gpu.temperature != null ? `${Math.round(gpu.temperature)}°C` : null, gpu.power_watts != null ? `${Math.round(gpu.power_watts)} W` : null].filter(Boolean).join(", ");
  return (
    <>
      <LineChart label={`${prefix}load`} samples={util} max={100} format={percent} windowSeconds={WINDOW_SECONDS} detail={[gpu.name, facts].filter(Boolean).join(", ")} />
      <LineChart
        label={`${prefix}memory`}
        samples={memory}
        max={(gpu.memory_total ?? GB) / GB}
        format={gigabytes}
        formatAxis={gigabytesAxis}
        windowSeconds={WINDOW_SECONDS}
        detail={gpu.memory_total ? `of ${formatBytes(gpu.memory_total)}` : undefined}
      />
    </>
  );
}

function RunningModels({ host }: { host: HostState }) {
  if (!host.online) return null;
  if (!host.models.length) return <p className="subtle">No models loaded. Ollama loads one on its first request.</p>;
  return (
    <div className="table-scroll">
      <table className="table">
        <caption className="visually-hidden">Loaded models, like ollama ps</caption>
        <thead>
          <tr>
            <th scope="col">Loaded model</th>
            <th scope="col" className="num">Size</th>
            <th scope="col">Processor</th>
            <th scope="col" className="num">Context</th>
            <th scope="col" className="num">Unloads in</th>
          </tr>
        </thead>
        <tbody>
          {host.models.map((model) => {
            const gpu = model.gpu_percent ?? 0;
            return (
              <tr key={model.name}>
                <td>
                  <strong className="mono">{model.name}</strong>
                  <small>{[model.parameter_size, model.quantization].filter(Boolean).join(", ")}</small>
                </td>
                <td className="num nowrap">{formatBytes(model.size)}</td>
                <td>
                  <span className={gpu < 100 ? "warn" : "good"}>{gpu >= 100 ? "100% GPU" : `${gpu}% GPU, ${100 - gpu}% CPU`}</span>
                  <span className="split-bar" aria-hidden="true">
                    <span style={{ width: `${gpu}%` }} />
                  </span>
                </td>
                <td className="num">{formatNumber(model.context_length)}</td>
                <td className="num nowrap">{countdown(model.expires_at)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function countdown(value: string | null) {
  if (!value) return "–";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  if (seconds > 10 * 365 * 24 * 3600) return "never";
  // Ollama only starts the keep-alive timer once a model is idle.
  if (seconds <= 0) return "when idle";
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${String(seconds % 60).padStart(2, "0")}s` : `${seconds}s`;
}
