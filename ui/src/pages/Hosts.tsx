import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type GpuSample, type MachineSample, type OllamaProviderModel, type OllamaState, type ProviderRef } from "../api";
import { LineChart, type Sample } from "../components/LineChart";
import { formatBytes, formatNumber } from "../format";
import { useToast } from "../hooks";
import { useTicker } from "../live";

const POLL_MS = 2000;
const WINDOW_SECONDS = 300;
const GB = 1024 ** 3;
const percent = (value: number) => `${Math.round(value)}%`;
const gigabytes = (value: number) => `${value.toFixed(1)} GB`;
const gigabytesAxis = (value: number) => (value === 0 ? "0 GB" : `${value.toFixed(value >= 10 ? 0 : 1)} GB`);

interface History {
  cpu: Sample[];
  memory: Sample[];
  gpuUtil: Sample[][];
  gpuMemory: Sample[][];
}

/** The last five minutes of samples, kept only while the page is open. */
function useLoadHistory(sample: MachineSample | undefined) {
  const history = useRef<History>({ cpu: [], memory: [], gpuUtil: [], gpuMemory: [] });
  const [, setVersion] = useState(0);
  const last = useRef(0);
  useEffect(() => {
    if (!sample || sample.timestamp === last.current) return;
    last.current = sample.timestamp;
    const cutoff = sample.timestamp - WINDOW_SECONDS;
    const push = (series: Sample[], v: number | null) => {
      series.push({ t: sample.timestamp, v });
      while (series.length && series[0].t < cutoff) series.shift();
    };
    const entry = history.current;
    push(entry.cpu, sample.cpu.percent);
    push(entry.memory, sample.memory.used / GB);
    sample.gpus.forEach((gpu, index) => {
      entry.gpuUtil[index] ??= [];
      entry.gpuMemory[index] ??= [];
      push(entry.gpuUtil[index], gpu.utilization);
      push(entry.gpuMemory[index], gpu.memory_used != null ? gpu.memory_used / GB : null);
    });
    setVersion((value) => value + 1);
  }, [sample]);
  return history.current;
}

/**
 * How hard a machine is working, sampled there by its agent-machine every two seconds while
 * this is open (nothing is saved), and what its Ollama servers have loaded, like `ollama ps`.
 */
export function MachineLoad({ machine, ollama }: { machine: string; ollama: { target: ProviderRef; label: string }[] }) {
  const metrics = useQuery({ queryKey: ["metrics", machine], queryFn: () => api.machineMetrics(machine), refetchInterval: POLL_MS, gcTime: 0 });
  const history = useLoadHistory(metrics.data);
  useTicker(true, 1000);
  const sample = metrics.data;
  return (
    <div className="tab-stack">
      {metrics.isPending && <p className="empty">Asking the machine for its load…</p>}
      {metrics.isError && <p className="bad">Couldn't read the machine's load: {metrics.error.message}</p>}
      {sample && (
        <>
          <p className="subtle">
            {sample.hostname}, {sample.platform}. Sampled there every {POLL_MS / 1000} seconds while this is open.
          </p>
          <div className="metric-grid-layout">
            <LineChart
              label="CPU"
              samples={history.cpu}
              max={100}
              format={percent}
              windowSeconds={WINDOW_SECONDS}
              detail={sample.cpu.count ? `${sample.cpu.count} cores` : undefined}
            />
            <LineChart
              label="Memory"
              samples={history.memory}
              max={sample.memory.total / GB}
              format={gigabytes}
              formatAxis={gigabytesAxis}
              windowSeconds={WINDOW_SECONDS}
              detail={`of ${formatBytes(sample.memory.total)}`}
            />
            {sample.gpus.map((gpu, index) => (
              <GpuCharts key={gpu.index} gpu={gpu} util={history.gpuUtil[index] ?? []} memory={history.gpuMemory[index] ?? []} multiple={sample.gpus.length > 1} />
            ))}
          </div>
          {sample.gpu_error && <p className="subtle">{sample.gpu_error}</p>}
        </>
      )}
      {ollama.map((item) => (
        <section key={item.target.provider} className="section">
          <div className="section-head">
            <h2>Loaded on {item.label}</h2>
          </div>
          <RunningModels target={item.target} />
        </section>
      ))}
    </div>
  );
}

function GpuCharts({ gpu, util, memory, multiple }: { gpu: GpuSample; util: Sample[]; memory: Sample[]; multiple: boolean }) {
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

function RunningModels({ target }: { target: ProviderRef }) {
  const key = ["provider", target.machine, target.provider];
  const state = useQuery({ queryKey: key, queryFn: () => api.providerStatus<OllamaState>(target), refetchInterval: POLL_MS, gcTime: 0 });
  const client = useQueryClient();
  const notify = useToast();
  const unload = useMutation({
    mutationFn: (model: string) => api.providerModelAction(target, "unload", model),
    onSuccess: (_, model) => {
      notify(`Unloaded ${model}`);
      client.invalidateQueries({ queryKey: key });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error: Error) => notify(error.message),
  });
  if (state.isPending) return <p className="subtle">Asking it…</p>;
  if (state.isError) return <p className="bad">{state.error.message}</p>;
  if (!state.data.online) return <p className="bad">Offline: {state.data.error ?? "no answer"}</p>;
  const loaded: OllamaProviderModel[] = state.data.models.filter((model) => model.loaded);
  if (!loaded.length) return <p className="subtle">No models loaded. Ollama loads one on its first request.</p>;
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
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {loaded.map((model) => {
            const gpu = model.gpu_percent ?? 0;
            return (
              <tr key={model.name}>
                <td>
                  <strong className="mono">{model.name}</strong>
                  <small>{[model.parameter_size, model.quantization].filter(Boolean).join(", ")}</small>
                </td>
                <td className="num nowrap">{formatBytes(model.size_loaded ?? model.size)}</td>
                <td>
                  <span className={gpu < 100 ? "warn" : "good"}>{gpu >= 100 ? "100% GPU" : `${gpu}% GPU, ${100 - gpu}% CPU`}</span>
                  <span className="split-bar" aria-hidden="true">
                    <span style={{ width: `${gpu}%` }} />
                  </span>
                </td>
                <td className="num">{formatNumber(model.context_length)}</td>
                <td className="num nowrap">{countdown(model.expires_at)}</td>
                <td>
                  <div className="model-actions">
                    <button type="button" disabled={unload.isPending && unload.variables === model.name} onClick={() => unload.mutate(model.name)}>
                      Unload
                    </button>
                  </div>
                </td>
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
