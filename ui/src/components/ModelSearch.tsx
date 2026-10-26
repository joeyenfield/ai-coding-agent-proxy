import { useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { api, pullModel, type HfSearch, type LibrarySearch, type ProviderRef, type PullProgress } from "../api";
import { formatBytes, formatNumber } from "../format";
import { useToast } from "../hooks";
import { TabBar, TabPanel } from "./Tabs";

const SOURCES = ["library", "hf", "name"] as const;
type Source = (typeof SOURCES)[number];

export interface Pull {
  ref: string;
  progress: PullProgress | null;
}

export interface Puller {
  pull: Pull | null;
  /** True while a pull is streaming; a failed pull stays in `pull` so its error shows until dismissed. */
  active: boolean;
  /** Where pulls go: one model server on one machine, which does the download. */
  target: ProviderRef;
  label: string;
  start: (ref: string) => Promise<boolean>;
  cancel: () => void;
  dismiss: () => void;
}

export const pullPercent = (progress: PullProgress | null) =>
  progress?.total ? Math.round(((progress.completed ?? 0) / progress.total) * 100) : null;

/** One pull at a time to one model server, owned by its page so it keeps running while the user switches tabs. */
export function usePull(target: ProviderRef, label: string, onPulled: () => void): Puller {
  const notify = useToast();
  const [pull, setPull] = useState<Pull | null>(null);
  const abort = useRef<AbortController | null>(null);

  const start = async (model: string) => {
    if (abort.current) {
      notify("Wait for the current pull to finish, or cancel it.");
      return false;
    }
    const controller = new AbortController();
    abort.current = controller;
    setPull({ ref: model, progress: { status: "starting" } });
    try {
      await pullModel(target, model, (progress) => setPull({ ref: model, progress }), controller.signal);
      notify(`Pulled ${model} to ${label}`);
      setPull(null);
      onPulled();
      return true;
    } catch (error) {
      if (controller.signal.aborted) {
        notify("Pull cancelled");
        setPull(null);
      } else {
        const message = (error as Error).message;
        notify(message);
        setPull((current) => (current ? { ...current, progress: { ...(current.progress ?? {}), error: message } } : current));
      }
      return false;
    } finally {
      abort.current = null;
    }
  };

  return {
    pull,
    active: pull != null && !pull.progress?.error,
    target,
    label,
    start,
    cancel: () => abort.current?.abort(),
    dismiss: () => setPull(null),
  };
}

export function PullStatus({ puller }: { puller: Puller }) {
  if (!puller.pull) return null;
  const { ref, progress } = puller.pull;
  const percent = pullPercent(progress);
  return (
    <div className="pull callout" role="status">
      <div className="row wrap">
        <strong className="mono">{ref}</strong>
        <span className="subtle">to {puller.label}</span>
        <span className={progress?.error ? "bad" : "subtle"}>
          {progress?.error ?? progress?.status ?? "Pulling…"}
          {!progress?.error && percent != null ? ` — ${percent}%` : ""}
          {!progress?.error && progress?.total ? ` (${formatBytes(progress.completed ?? 0)} / ${formatBytes(progress.total)})` : ""}
        </span>
        <button type="button" className="link" onClick={puller.active ? puller.cancel : puller.dismiss}>
          {puller.active ? "Cancel" : "Dismiss"}
        </button>
      </div>
      {puller.active && percent != null && (
        <div className="pull-bar">
          <span style={{ width: `${percent}%` }} />
        </div>
      )}
    </div>
  );
}

/** Search the Ollama library or Hugging Face for a model, or name one, and pull it to the puller's model server. Its machine searches and pulls. */
export function ModelSearch({ puller, disabled }: { puller: Puller; disabled?: string | null }) {
  const [source, setSource] = useState<Source>("library");
  const [input, setInput] = useState("");
  const [submitted, setSubmitted] = useState("");
  const ids = useId();
  const machine = puller.target.machine;

  const library = useQuery({
    queryKey: ["library-search", machine, submitted],
    queryFn: () => api.searchLibrary(machine, submitted),
    enabled: source === "library" && submitted.length > 0,
  });
  const hf = useQuery({
    queryKey: ["hf-search", machine, submitted],
    queryFn: () => api.searchHuggingFace(machine, submitted),
    enabled: source === "hf" && submitted.length > 0,
  });

  const onPull = (ref: string) => void puller.start(ref);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = input.trim();
    if (source !== "name") setSubmitted(text);
    else if (await puller.start(text)) setInput("");
  };
  const busy = puller.active || Boolean(disabled);

  return (
    <div className="tab-stack">
      {disabled && <p className="callout">{disabled}</p>}
      <TabBar
        label="Model source"
        idPrefix={ids}
        className="flush"
        active={source}
        onSelect={setSource}
        tabs={[
          { id: "library", label: "Ollama library" },
          { id: "hf", label: "Hugging Face (GGUF)" },
          { id: "name", label: "By name" },
        ]}
      />

      <form className="filters" onSubmit={(event) => void submit(event)}>
        <input
          type="search"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder={
            source === "library" ? "Search ollama.com, e.g. qwen3"
            : source === "hf" ? "Search Hugging Face GGUF, e.g. qwen2.5 coder"
            : "A tag such as qwen3:8b, or hf.co/user/repo:Q4_K_M"
          }
          aria-label={source === "name" ? "Model to pull" : "Search for a model"}
        />
        <button type="submit" className="primary" disabled={!input.trim() || (source === "name" && busy)}>
          {source === "name" ? "Pull" : "Search"}
        </button>
        <span className="subtle pull-target">Pulls to {puller.label}</span>
      </form>

      <TabPanel idPrefix={ids} id="library" active={source === "library"}>
        <LibraryResults query={submitted} result={library} onPull={onPull} busy={busy} />
      </TabPanel>
      <TabPanel idPrefix={ids} id="hf" active={source === "hf"}>
        <HfResults query={submitted} machine={machine} result={hf} onPull={onPull} busy={busy} />
      </TabPanel>
      <TabPanel idPrefix={ids} id="name" active={source === "name"}>
        <p className="subtle">
          Pull any tag from <a href="https://ollama.com/library" target="_blank" rel="noreferrer">ollama.com/library</a>, or a GGUF repo
          from Hugging Face as <code>hf.co/user/repo:quant</code>.
        </p>
      </TabPanel>
    </div>
  );
}

/** The placeholder to show instead of results, or null when results should render. */
function searchPlaceholder(query: string, result: UseQueryResult<{ note: string | null }>, hint: string): ReactNode | null {
  if (!query) return <p className="empty">{hint}</p>;
  if (result.isPending) return <p className="empty">Searching…</p>;
  if (result.isError) return <p className="bad">{(result.error as Error).message}</p>;
  if (result.data?.note) return <p className="empty">{result.data.note}</p>;
  return null;
}

function LibraryResults({
  query,
  result,
  onPull,
  busy,
}: {
  query: string;
  result: UseQueryResult<LibrarySearch>;
  onPull: (ref: string) => void;
  busy: boolean;
}) {
  const placeholder = searchPlaceholder(query, result, "Search ollama.com by model family, such as qwen3, gemma3 or gpt-oss.");
  if (placeholder) return placeholder;
  return (
    <ul className="search-results">
      {result.data!.models.map((model) => (
        <li key={model.name} className="search-item">
          <div className="search-item-main">
            <strong className="mono">{model.name}</strong>
            {model.description && <small>{model.description}</small>}
            <div className="search-meta">
              {model.capabilities.map((capability) => (
                <span key={capability} className="pill">
                  {capability}
                </span>
              ))}
              {model.sizes.length > 0 && <span className="subtle">{model.sizes.join(" · ")}</span>}
              {model.pulls != null && <span className="subtle">{formatNumber(model.pulls)} pulls</span>}
            </div>
          </div>
          <div className="model-actions">
            {model.sizes.length ? (
              model.sizes.map((size) => (
                <button key={size} type="button" disabled={busy} onClick={() => onPull(`${model.name}:${size}`)}>
                  Pull {size}
                </button>
              ))
            ) : (
              <button type="button" disabled={busy} onClick={() => onPull(model.name)}>
                Pull latest
              </button>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function HfResults({
  query,
  machine,
  result,
  onPull,
  busy,
}: {
  query: string;
  machine: string;
  result: UseQueryResult<HfSearch>;
  onPull: (ref: string) => void;
  busy: boolean;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const placeholder = searchPlaceholder(query, result, "Search Hugging Face for GGUF repos, then pick a quantization to pull.");
  if (placeholder) return placeholder;
  return (
    <ul className="search-results">
      {result.data!.repos.map((item) => (
        <li key={item.repo} className="search-item hf">
          <div className="search-item-main">
            <button type="button" className="link mono" onClick={() => setExpanded(expanded === item.repo ? null : item.repo)}>
              {item.repo}
            </button>
            <div className="search-meta">
              {item.downloads != null && <span className="subtle">{formatNumber(item.downloads)} downloads</span>}
              {item.likes != null && <span className="subtle">{formatNumber(item.likes)} likes</span>}
            </div>
            {expanded === item.repo && <HfQuantPicker machine={machine} repo={item.repo} onPull={onPull} busy={busy} />}
          </div>
          <div className="model-actions">
            <button type="button" onClick={() => setExpanded(expanded === item.repo ? null : item.repo)}>
              {expanded === item.repo ? "Hide quants" : "Show quants"}
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}

function HfQuantPicker({ machine, repo, onPull, busy }: { machine: string; repo: string; onPull: (ref: string) => void; busy: boolean }) {
  const quants = useQuery({ queryKey: ["hf-quants", machine, repo], queryFn: () => api.hfQuants(machine, repo) });
  if (quants.isPending) return <p className="subtle">Reading quantizations…</p>;
  if (quants.isError) return <p className="bad">{(quants.error as Error).message}</p>;
  if (quants.data.note) return <p className="subtle">{quants.data.note}</p>;
  return (
    <div className="search-quants">
      {quants.data.quants.map((quant) => (
        <button key={quant.quant} type="button" disabled={busy} className="quant" onClick={() => onPull(`hf.co/${repo}:${quant.quant}`)}>
          <span className="mono">{quant.quant}</span>
          <small>
            {formatBytes(quant.size)}
            {quant.split ? " · split" : ""}
          </small>
        </button>
      ))}
    </div>
  );
}
