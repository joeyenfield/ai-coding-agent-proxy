import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type OllamaProviderModel, type OllamaState, type Profile, type ProviderRef } from "../api";
import { formatBytes, formatNumber } from "../format";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ModelSearch, PullStatus, pullPercent, usePull } from "../components/ModelSearch";
import { TabBar, TabCount, TabPanel, useTabParam } from "../components/Tabs";
import { useModels, useToast } from "../hooks";

const TABS = ["models", "find", "aliases"] as const;
const POLL_MS = 3000;
type SortKey = "name" | "size" | "memory";
type Sort = { key: SortKey; dir: 1 | -1 };

function compareModels(sort: Sort, a: OllamaProviderModel, b: OllamaProviderModel): number {
  if (sort.key === "name") return a.name.localeCompare(b.name) * sort.dir;
  if (sort.key === "size") return ((a.size ?? 0) - (b.size ?? 0)) * sort.dir;
  // Memory: loaded models (by VRAM used) sort ahead of unloaded ones.
  const av = a.loaded ? a.size_vram ?? 0 : -1;
  const bv = b.loaded ? b.size_vram ?? 0 : -1;
  return (av - bv) * sort.dir;
}

/**
 * One Ollama on one machine: its models (load, unload, delete), finding and pulling more, and
 * the aliases agents can use. The machine does all of it (agent-machine). backend names the
 * backend agents use it as, whose model tuning the server adds to their requests;
 * manageable, when set, says why changes can't be made now.
 */
export function OllamaProvider({ target, label, backend, manageable }: { target: ProviderRef; label: string; backend: string | null; manageable: string | null }) {
  const key = ["provider", target.machine, target.provider];
  const state = useQuery({ queryKey: key, queryFn: () => api.providerStatus<OllamaState>(target), refetchInterval: POLL_MS });
  const listing = useModels();
  const client = useQueryClient();
  const [tab, setTab] = useTabParam(TABS);
  const tabs = useId();
  const puller = usePull(target, label, () => {
    client.invalidateQueries({ queryKey: key });
    client.invalidateQueries({ queryKey: ["models"] });
  });
  const percent = pullPercent(puller.pull?.progress ?? null);
  const profiles = new Map((backend ? listing.data?.backends[backend]?.models ?? [] : []).map((model) => [model.name, model.profile]));
  const aliases = backend ? Object.entries(listing.data?.aliases ?? {}) : [];

  if (state.isPending) return <p className="empty">Asking {label} for its models…</p>;
  if (state.isError) return <p className="bad">{state.error.message}</p>;
  const data = state.data;
  return (
    <>
      <ModelTotals state={data} />
      <div className="tabbed">
        <TabBar
          label="Ollama sections"
          idPrefix={tabs}
          className="page-tabs"
          active={tab}
          onSelect={setTab}
          tabs={[
            { id: "models", label: "Models", badge: <TabCount value={data.models.length} /> },
            {
              id: "find",
              label: "Find and pull",
              badge: puller.active ? <TabCount tone="live" value={percent != null ? `${percent}%` : "…"} /> : undefined,
            },
            ...(backend ? [{ id: "aliases" as const, label: "Aliases", badge: <TabCount value={aliases.length} /> }] : []),
          ]}
        />
        <PullStatus puller={puller} />
        <TabPanel idPrefix={tabs} id="models" active={tab === "models"} keepMounted>
          <ModelTable state={data} target={target} label={label} profiles={profiles} manageable={manageable} onFind={() => setTab("find")} />
        </TabPanel>
        <TabPanel idPrefix={tabs} id="find" active={tab === "find"} keepMounted>
          <ModelSearch puller={puller} disabled={data.online ? manageable : `${label} is offline.`} />
        </TabPanel>
        {backend && (
          <TabPanel idPrefix={tabs} id="aliases" active={tab === "aliases"} keepMounted>
            <AliasesTab aliases={aliases} />
          </TabPanel>
        )}
      </div>
    </>
  );
}

function ModelTotals({ state }: { state: OllamaState }) {
  const loaded = state.models.filter((model) => model.loaded);
  const resident = loaded.reduce((total, model) => total + (model.size_loaded ?? 0), 0);
  const vram = loaded.reduce((total, model) => total + (model.size_vram ?? 0), 0);
  const traffic = state.traffic;
  return (
    <dl className="stat-cards">
      <div>
        <dt>Ollama</dt>
        <dd className={state.online ? undefined : "bad"}>{state.online ? state.version ?? "Online" : "Offline"}</dd>
      </div>
      <div>
        <dt>Models</dt>
        <dd>
          {state.models.length} <small>{formatBytes(state.models.reduce((total, model) => total + (model.size ?? 0), 0))} on disk</small>
        </dd>
      </div>
      <div>
        <dt>Loaded now</dt>
        <dd>
          {loaded.length} {resident > 0 && <small>{formatBytes(resident)}, {Math.round((vram / resident) * 100)}% on GPU</small>}
        </dd>
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

function ModelTable({
  state,
  target,
  label,
  profiles,
  manageable,
  onFind,
}: {
  state: OllamaState;
  target: ProviderRef;
  label: string;
  profiles: Map<string, Profile>;
  manageable: string | null;
  onFind: () => void;
}) {
  const client = useQueryClient();
  const notify = useToast();
  const [query, setQuery] = useState("");
  const [loadedOnly, setLoadedOnly] = useState(false);
  const [sort, setSort] = useState<Sort>({ key: "name", dir: 1 });
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const refresh = () => {
    client.invalidateQueries({ queryKey: ["provider", target.machine, target.provider] });
    client.invalidateQueries({ queryKey: ["models"] });
  };
  const load = useMutation({
    mutationFn: (model: string) => api.providerModelAction(target, "load", model),
    onSuccess: (_, model) => { notify(`Loaded ${model}`); refresh(); },
    onError: (error: Error) => notify(error.message),
  });
  const unload = useMutation({
    mutationFn: (model: string) => api.providerModelAction(target, "unload", model),
    onSuccess: (_, model) => { notify(`Unloaded ${model}`); refresh(); },
    onError: (error: Error) => notify(error.message),
  });
  const remove = useMutation({
    mutationFn: (model: string) => api.providerModelAction(target, "delete", model),
    onSuccess: (_, model) => { notify(`Deleted ${model}`); refresh(); },
    onError: (error: Error) => notify(error.message),
    onSettled: () => setConfirmDelete(null),
  });
  const working = (model: string) =>
    Boolean(manageable) || (load.isPending && load.variables === model) || (unload.isPending && unload.variables === model) || (remove.isPending && remove.variables === model);
  const text = query.trim().toLowerCase();
  const toggleSort = (key: SortKey) => setSort((current) => (current.key === key ? { key, dir: current.dir === 1 ? -1 : 1 } : { key, dir: 1 }));
  const visible = state.models
    .filter((model) => (!text || model.name.toLowerCase().includes(text)) && (!loadedOnly || model.loaded))
    .sort((a, b) => compareModels(sort, a, b));

  if (!state.online) return <p className="bad">Offline: {state.error ?? "no answer"}</p>;
  if (!state.models.length) {
    return (
      <div className="empty">
        <p>No models on {label} yet.</p>
        <button type="button" className="primary" onClick={onFind}>
          Find a model to pull
        </button>
      </div>
    );
  }
  return (
    <div className="tab-stack">
      {manageable && <p className="callout">{manageable}</p>}
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter models" aria-label="Filter models" />
        <label className="check">
          <input type="checkbox" checked={loadedOnly} onChange={(event) => setLoadedOnly(event.target.checked)} />
          Loaded in memory only
        </label>
      </div>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <SortHeader label="Model" col="name" sort={sort} onSort={toggleSort} />
              <SortHeader label="Size" col="size" sort={sort} onSort={toggleSort} className="num" />
              <SortHeader label="Memory" col="memory" sort={sort} onSort={toggleSort} />
              <th scope="col">Server settings</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((model) => {
              const profile = profiles.get(model.name);
              return (
                <tr key={model.name}>
                  <td>
                    <strong className="mono">{model.name}</strong>
                    <small>{[model.family, model.parameter_size, model.quantization].filter(Boolean).join(", ")}</small>
                  </td>
                  <td className="num nowrap">{formatBytes(model.size)}</td>
                  <td>{model.loaded ? <GpuShare model={model} /> : <span className="subtle nowrap">Not loaded</span>}</td>
                  <td>{profile ? <ProfileSummary profile={profile} /> : <span className="subtle">Add it as a provider to apply the model tuning</span>}</td>
                  <td>
                    <div className="model-actions">
                      {model.loaded ? (
                        <button type="button" disabled={working(model.name)} onClick={() => unload.mutate(model.name)}>
                          Unload
                        </button>
                      ) : (
                        <button type="button" disabled={working(model.name)} onClick={() => load.mutate(model.name)}>
                          Load
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
        {!visible.length && <p className="empty">No models match.</p>}
      </div>
      <ConfirmDialog
        open={confirmDelete !== null}
        title="Delete model?"
        confirmLabel={remove.isPending ? "Deleting…" : "Delete model"}
        busy={remove.isPending}
        onConfirm={() => confirmDelete && remove.mutate(confirmDelete)}
        onCancel={() => setConfirmDelete(null)}
      >
        <p>
          Permanently delete <strong className="mono">{confirmDelete}</strong> from <strong>{label}</strong>? Blobs shared with other
          models are kept. You can pull it again later.
        </p>
      </ConfirmDialog>
    </div>
  );
}

function AliasesTab({ aliases }: { aliases: [string, Profile][] }) {
  if (!aliases.length) {
    return (
      <p className="empty">
        No aliases yet. Add one under <code>aliases:</code> in <Link to="/settings">Server settings</Link>' Ollama model tuning to give
        agents a short name for an Ollama tag and its settings.
      </p>
    );
  }
  return (
    <div className="tab-stack">
      <p className="subtle">
        Names agents can use in place of an Ollama tag, on every Ollama provider. Edit them under <code>aliases:</code> in{" "}
        <Link to="/settings">Server settings</Link>' Ollama model tuning.
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Alias</th>
              <th scope="col">Ollama model</th>
              <th scope="col">Server settings</th>
            </tr>
          </thead>
          <tbody>
            {aliases.map(([alias, profile]) => (
              <tr key={alias}>
                <td className="mono">{alias}</td>
                <td className="mono">{profile.ollama_model}</td>
                <td>
                  <ProfileSummary profile={profile} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SortHeader({ label, col, sort, onSort, className }: { label: string; col: SortKey; sort: Sort; onSort: (key: SortKey) => void; className?: string }) {
  const active = sort.key === col;
  return (
    <th scope="col" className={className} aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
      <button type="button" className="sort-th" onClick={() => onSort(col)}>
        {label}
        <span className="sort-caret" aria-hidden="true">{active ? (sort.dir === 1 ? "▲" : "▼") : ""}</span>
      </button>
    </th>
  );
}

/** Ollama reports the loaded size and the part of it in VRAM; anything else runs on the CPU. */
function GpuShare({ model }: { model: OllamaProviderModel }) {
  const total = model.size_loaded ?? 0;
  const share = total && model.size_vram != null ? Math.round((model.size_vram / total) * 100) : null;
  const spilled = share != null && share < 100;
  return (
    <>
      <span className={spilled ? "warn" : "good"}>
        {share == null ? "Loaded" : spilled ? `Loaded, ${share}% on GPU` : "Loaded on GPU"}
      </span>
      <small>
        {formatNumber(model.context_length)} context
        {spilled ? ". The rest runs on the CPU and is much slower; lower num_ctx or use a smaller model." : ""}
      </small>
    </>
  );
}

function ProfileSummary({ profile }: { profile: Profile }) {
  const { num_ctx, ...rest } = profile.options as Record<string, unknown> & { num_ctx?: number };
  const parts = [
    num_ctx ? `${formatNumber(Number(num_ctx))} context` : null,
    profile.think === false ? "thinking off" : profile.think === true ? "thinking on" : null,
    ...Object.entries(rest).map(([key, value]) => `${key} ${value}`),
  ].filter(Boolean);
  return (
    <>
      <span>{parts.join(", ") || "Passed through unchanged"}</span>
      <small>From {profile.matched.join(", then ") || "no profile"}</small>
    </>
  );
}
