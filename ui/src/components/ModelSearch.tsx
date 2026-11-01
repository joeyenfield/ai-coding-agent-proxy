import { useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import {
  api,
  pullModel,
  PullConnectionLost,
  type HfSearch,
  type LibrarySearch,
  type LlamaFilesPlan,
  type ProviderRef,
  type PullMethod,
  type PullProgress,
} from "../api";
import { formatBytes, formatNumber } from "../format";
import { useClipboard, useToast } from "../hooks";
import { CodeLine } from "./CodeLine";
import { TabBar, TabPanel } from "./Tabs";

const SOURCES = ["library", "hf", "name", "manual"] as const;
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
  /** method is llama.cpp's: through its router, or agent-machine downloading the files. */
  start: (ref: string, method?: PullMethod) => Promise<boolean>;
  cancel: () => void;
  dismiss: () => void;
}

export const pullPercent = (progress: PullProgress | null) =>
  progress?.total ? Math.round(((progress.completed ?? 0) / progress.total) * 100) : null;

/** One pull at a time to one model server, owned by its page so it keeps running while the user switches tabs. */
export function usePull(target: ProviderRef, label: string, onPulled: () => void): Puller {
  const notify = useToast();
  const client = useQueryClient();
  const [pull, setPull] = useState<Pull | null>(null);
  const abort = useRef<AbortController | null>(null);

  const start = async (model: string, method: PullMethod = "") => {
    if (abort.current) {
      notify("Wait for the current pull to finish, or cancel it.");
      return false;
    }
    const controller = new AbortController();
    abort.current = controller;
    setPull({ ref: model, progress: { status: "starting" } });
    try {
      await pullModel(target, model, (progress) => setPull({ ref: model, progress }), controller.signal, method);
      notify(`Pulled ${model} to ${label}`);
      setPull(null);
      onPulled();
      return true;
    } catch (error) {
      if (controller.signal.aborted) {
        notify("Pull cancelled");
        setPull(null);
      } else if (error instanceof PullConnectionLost) {
        // Not a failure: the machine may be finishing it. Refresh the list, which shows it once it's there.
        notify(error.message);
        setPull((current) => (current ? { ...current, progress: { ...(current.progress ?? {}), error: error.message } } : current));
        client.invalidateQueries({ queryKey: ["provider", target.machine, target.provider] });
        onPulled();
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

/**
 * Search the Ollama library or Hugging Face for a model, or name one, and pull it to the puller's model server. Its machine searches and pulls.
 * For llama.cpp (flavor "llamacpp") there's no Ollama library, and a Hugging Face model is named user/repo:quant, as llama-server takes it.
 */
export function ModelSearch({ puller, disabled, flavor = "ollama" }: { puller: Puller; disabled?: string | null; flavor?: "ollama" | "llamacpp" }) {
  const llama = flavor === "llamacpp";
  const [source, setSource] = useState<Source>(llama ? "hf" : "library");
  const [input, setInput] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [method, setMethod] = useState<PullMethod>("");
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

  const onPull = (ref: string) => void puller.start(ref, method);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = input.trim();
    if (source !== "name") setSubmitted(text);
    else if (await puller.start(text, method)) setInput("");
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
          ...(llama ? [] : [{ id: "library" as const, label: "Ollama library" }]),
          { id: "hf", label: "Hugging Face (GGUF)" },
          { id: "name", label: "By name" },
          ...(llama ? [{ id: "manual" as const, label: "By hand" }] : []),
        ]}
      />

      {source === "manual" ? (
        <ManualDownload puller={puller} busy={busy} />
      ) : (
      <form className="filters" onSubmit={(event) => void submit(event)}>
        <input
          type="search"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder={
            source === "library" ? "Search ollama.com, e.g. qwen3"
            : source === "hf" ? "Search Hugging Face GGUF, e.g. qwen2.5 coder"
            : llama ? "A Hugging Face GGUF repo, such as ggml-org/gemma-3-4b-it-GGUF:Q4_K_M"
            : "A tag such as qwen3:8b, or hf.co/user/repo:Q4_K_M"
          }
          aria-label={source === "name" ? "Model to pull" : "Search for a model"}
        />
        <button type="submit" className="primary" disabled={!input.trim() || (source === "name" && busy)}>
          {source === "name" ? "Pull" : "Search"}
        </button>
        {llama && (
          <select value={method} onChange={(event) => setMethod(event.target.value as PullMethod)} aria-label="How the machine downloads it">
            <option value="">Through llama.cpp (the machine itself if llama.cpp can't)</option>
            <option value="direct">The machine downloads the files</option>
          </select>
        )}
        <span className="subtle pull-target">Pulls to {puller.label}</span>
      </form>
      )}

      <TabPanel idPrefix={ids} id="library" active={source === "library"}>
        <LibraryResults query={submitted} result={library} onPull={onPull} busy={busy} />
      </TabPanel>
      <TabPanel idPrefix={ids} id="hf" active={source === "hf"}>
        <HfResults query={submitted} machine={machine} result={hf} onPull={onPull} busy={busy} prefix={llama ? "" : "hf.co/"} />
      </TabPanel>
      <TabPanel idPrefix={ids} id="name" active={source === "name"}>
        {llama ? (
          <p className="subtle">
            Download any GGUF repo from <a href="https://huggingface.co/models?library=gguf" target="_blank" rel="noreferrer">Hugging Face</a> as{" "}
            <code>user/repo:quant</code>, such as <code>unsloth/Qwen3-8B-GGUF:Q4_K_M</code>. Without a quant, llama.cpp picks Q4_K_M. A
            vision model's projector comes with it. llama.cpp downloads it itself. If it's too old to, or you choose so beside Pull, the
            machine downloads the files into llama.cpp's GGUF folder instead, checks them by SHA-256, and carries on where a stopped
            download left off.
          </p>
        ) : (
          <p className="subtle">
            Pull any tag from <a href="https://ollama.com/library" target="_blank" rel="noreferrer">ollama.com/library</a>, or a GGUF repo
            from Hugging Face as <code>hf.co/user/repo:quant</code>.
          </p>
        )}
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
  prefix,
}: {
  query: string;
  machine: string;
  result: UseQueryResult<HfSearch>;
  onPull: (ref: string) => void;
  busy: boolean;
  /** Before user/repo:quant in the name pulled: Ollama's hf.co/, nothing for llama.cpp. */
  prefix: string;
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
            {expanded === item.repo && <HfQuantPicker machine={machine} repo={item.repo} onPull={onPull} busy={busy} prefix={prefix} />}
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

function HfQuantPicker({ machine, repo, onPull, busy, prefix }: { machine: string; repo: string; onPull: (ref: string) => void; busy: boolean; prefix: string }) {
  const quants = useQuery({ queryKey: ["hf-quants", machine, repo], queryFn: () => api.hfQuants(machine, repo) });
  if (quants.isPending) return <p className="subtle">Reading quantizations…</p>;
  if (quants.isError) return <p className="bad">{(quants.error as Error).message}</p>;
  if (quants.data.note) return <p className="subtle">{quants.data.note}</p>;
  return (
    <div className="search-quants">
      {quants.data.quants.map((quant) => (
        <button key={quant.quant} type="button" disabled={busy} className="quant" onClick={() => onPull(`${prefix}${repo}:${quant.quant}`)}>
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

/** The commands that download one llama.cpp model's files into the machine's GGUF folder (curl, resumable). */
export function llamaCurlCommands(plan: LlamaFilesPlan): string[] {
  const sep = plan.folder.includes("\\") && !plan.folder.includes("/") ? "\\" : "/";
  return plan.files.map((file) => `curl -L -C - --create-dirs -o "${plan.folder}${sep}${file.path}" "${file.url}"`);
}

/**
 * Downloading a llama.cpp model by hand: the machine says which files it is and where they go,
 * the user runs the commands on the machine, then Reload has its router list it.
 */
function ManualDownload({ puller, busy }: { puller: Puller; busy: boolean }) {
  const [input, setInput] = useState("");
  const client = useQueryClient();
  const copy = useClipboard();
  const notify = useToast();
  const plan = useMutation({ mutationFn: (name: string) => api.llamaFiles(puller.target, name) });
  const reload = useMutation({
    mutationFn: () => api.llamaReload(puller.target),
    onSuccess: () => {
      notify(`${puller.label} read its GGUF folder again`);
      client.invalidateQueries({ queryKey: ["provider", puller.target.machine, puller.target.provider] });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error) => notify((error as Error).message),
  });
  const data = plan.data;
  const commands = data ? llamaCurlCommands(data) : [];
  const total = data ? data.files.reduce((sum, file) => sum + file.size, 0) : 0;
  return (
    <div className="tab-stack">
      <p className="subtle">
        Download a model's GGUF files yourself, with curl or any other downloader, into the folder the machine's llama.cpp serves. Use it when a
        download through the machine doesn't work, or to download on a better connection and copy the files over.
      </p>
      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          if (input.trim()) plan.mutate(input.trim());
        }}
      >
        <input
          type="search"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          placeholder="A Hugging Face GGUF repo, such as unsloth/Qwen3-8B-GGUF:Q4_K_M"
          aria-label="Model to download by hand"
        />
        <button type="submit" disabled={!input.trim() || plan.isPending}>
          {plan.isPending ? "Asking Hugging Face…" : "Show the files"}
        </button>
      </form>
      {plan.isError && <p className="bad">{plan.error.message}</p>}
      {data && (
        <>
          {!data.serves_folder && (
            <p className="callout">
              This llama-server isn't the one agent-machine starts, so it doesn't serve this folder. Put the files in its own GGUF folder (its{" "}
              <code>--models-dir</code>) instead, then reload.
            </p>
          )}
          <p>
            <strong className="mono">{data.model}</strong>: {data.files.length} {data.files.length === 1 ? "file" : "files"}, {formatBytes(total)}, into{" "}
            <code>{data.dir}</code>. llama.cpp lists it as <code>{data.name}</code>.
          </p>
          <p className="subtle">
            Run these on the machine. <code>-C -</code> carries on a download that stopped. For a gated repo, add{" "}
            <code>-H "Authorization: Bearer &lt;your Hugging Face token&gt;"</code>. In Windows PowerShell, type <code>curl.exe</code>.
          </p>
          {commands.map((command) => (
            <CodeLine key={command} text={command} onCopy={() => void copy(command, "Command copied")} />
          ))}
          <details>
            <summary>Each file's address and SHA-256</summary>
            <ul className="plain">
              {data.files.map((file) => (
                <li key={file.path}>
                  <a href={file.url} target="_blank" rel="noreferrer" className="mono">
                    {file.repo_path}
                  </a>{" "}
                  <span className="subtle">
                    {formatBytes(file.size)}
                    {file.sha256 && (
                      <>
                        {" "}
                        · SHA-256 <span className="mono">{file.sha256}</span>
                      </>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </details>
          <div className="row wrap">
            <button type="button" className="primary" disabled={reload.isPending} onClick={() => reload.mutate()}>
              {reload.isPending ? "Reloading…" : "Reload, once the files are there"}
            </button>
            <button type="button" disabled={busy} onClick={() => void puller.start(data.model, "direct")}>
              Have the machine download them instead
            </button>
          </div>
        </>
      )}
    </div>
  );
}
