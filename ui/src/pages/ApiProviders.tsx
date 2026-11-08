import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  API_FLAVORS,
  ApiError,
  PROTOCOL_LABELS,
  type ApiAuthMode,
  type ApiCategory,
  type ApiPreset,
  type ApiProtocol,
  type ApiProvider,
  type ApiProviderFields,
  type ApiProviderSave,
  type ApiProviderTest,
  type ApiProvidersResponse,
  type MachineProvider,
  type Org,
} from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Icon } from "../components/Icon";
import { StateDot } from "../components/ProviderState";
import { formatRelative } from "../format";
import { useAuth, useMachines, useModels } from "../hooks";
import { DeepSeekSetup } from "./DeepSeekSetup";

/**
 * OpenAI- and Anthropic-compatible APIs (backend type "api"): an organization's API providers tab,
 * where its admins (and site admins) add, change, test and remove them and choose the teams that may
 * use each, and the read-only view of one a machine runs (LM Studio, vLLM, SGLang). The server keeps
 * every option and the keys; the browser only ever sends a new key and never gets one back.
 */

export const CATEGORY_LABELS: Record<ApiCategory, { title: string; summary: string }> = {
  machine: { title: "On machines", summary: "OpenAI-compatible servers your machines run, such as LM Studio, vLLM and SGLang. Add them from the machine's page." },
  hosted: { title: "Hosted APIs", summary: "A provider's own API, such as OpenAI, Anthropic, Groq or DeepSeek." },
  gateway: { title: "Gateways", summary: "Services that pass calls on to many providers, such as OpenRouter or a LiteLLM Proxy." },
};

const CATEGORY_ORDER: ApiCategory[] = ["hosted", "gateway", "machine"];

export const AUTH_LABELS: Record<ApiAuthMode, string> = {
  none: "No key",
  bearer: "Bearer token (Authorization)",
  "x-api-key": "x-api-key header (Anthropic)",
  "api-key": "api-key header (Azure)",
  agent: "The agent's own key",
};

const protocolLabel = (protocol: string) => PROTOCOL_LABELS[protocol as ApiProtocol] ?? protocol;

const KEY_ENV_PREFIX = "AI_PROXY_API_KEY_";

function forbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/** The editable part of a provider, as the PUT takes it. */
function fieldsOf(provider: ApiProvider): ApiProviderFields {
  return {
    url: provider.url,
    anthropic_url: provider.anthropic_url,
    category: provider.category,
    protocols: provider.protocols,
    auth: provider.auth,
    key_env: provider.key_env,
    models: provider.models,
    discover: provider.discover,
    headers: provider.headers,
    connect_timeout: provider.connect_timeout,
    verify_tls: provider.verify_tls,
    api_version: provider.api_version,
    priority: provider.priority,
    fallback: provider.fallback,
    enabled: provider.enabled,
    org: provider.org,
    teams: provider.teams,
    orgs: provider.orgs,
  };
}

const BLANK: ApiProviderFields = {
  url: "",
  anthropic_url: null,
  category: "hosted",
  protocols: ["openai-chat"],
  auth: "bearer",
  key_env: null,
  models: {},
  discover: true,
  headers: {},
  connect_timeout: 10,
  verify_tls: true,
  api_version: null,
  priority: 0,
  fallback: [],
  enabled: true,
  org: null,
  teams: [],
  orgs: [],
};

function fromPreset(preset: ApiPreset): ApiProviderFields {
  return {
    ...BLANK,
    url: preset.url,
    anthropic_url: preset.anthropic_url,
    category: preset.category,
    protocols: preset.protocols,
    auth: preset.auth,
    api_version: preset.api_version,
    headers: preset.headers,
  };
}

/** A name for a new provider from a preset's id: letters, numbers, _ and -, not one already taken. */
function freeName(base: string, taken: string[]): string {
  const clean = base.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "api";
  let name = clean;
  for (let index = 2; taken.includes(name); index++) name = `${clean}-${index}`;
  return name;
}

function healthText(health: ApiProvider["health"]): { tone: "good" | "bad" | "idle"; text: string } {
  if (!health) return { tone: "idle", text: "Not checked yet" };
  const when = formatRelative(new Date(health.at * 1000).toISOString());
  if (health.ok) return { tone: "good", text: `Answered${health.latency_ms != null ? ` in ${health.latency_ms} ms` : ""}, ${when}` };
  return { tone: "bad", text: `${health.error ?? (health.status ? `Answered ${health.status}` : "Didn't answer")}, ${when}` };
}

function keyText(provider: ApiProvider): string {
  if (provider.auth === "none") return "No key needed";
  if (provider.auth === "agent") return "Uses the agent's own key";
  if (provider.key_source === "vault") return "Key stored";
  if (provider.key_source === "env") return `Key from ${provider.key_env}`;
  if (provider.key_env) return `No key: ${provider.key_env} isn't set on the server`;
  return "No key set";
}

/** Who may use a provider, in words. */
function audience(provider: ApiProvider, org: Org): string {
  const teams = provider.teams.map((key) => org.teams.find((team) => team.key === key)?.name ?? key);
  const own = teams.length ? `People in ${teams.join(", ")}, and ${org.name}'s admins` : `Everyone in ${org.name}`;
  if (provider.orgs.includes("*")) return `${own}; also every other organization`;
  const others = provider.orgs.filter((item) => item !== org.id);
  return others.length ? `${own}; also ${others.join(", ")}` : own;
}

/** An organization's API providers tab (its admins and site admins; it hides itself from anyone the server refuses). */
export function ApiProviderSettings({ org }: { org: Org }) {
  const query = useQuery({ queryKey: ["api-providers", org.id], queryFn: () => api.apiProviders(org.id), refetchInterval: 300000 });
  const [adding, setAdding] = useState<{ name: string; fields: ApiProviderFields; preset: ApiPreset | null } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  if (forbidden(query.error) || (query.error instanceof ApiError && query.error.status === 404)) return null;
  const data = query.data;
  const providers = data?.providers ?? [];
  const names = providers.map((item) => item.name);
  return (
    <section className="section" aria-labelledby="api-providers-heading">
      <div className="section-head">
        <h2 id="api-providers-heading">API providers</h2>
      </div>
      <p className="subtle">
        OpenAI- and Anthropic-compatible APIs {org.name}'s agents can use: hosted ones, gateways such as OpenRouter or a LiteLLM Proxy, and servers on
        its machines. Choose which teams may use each. Agents name a model by its alias here, and the server sends the provider's own model id. Keys
        stay on the server and are never shown again.
      </p>
      {query.isError && <p className="bad">{query.error.message}</p>}
      {query.isPending && <p className="subtle">Loading…</p>}
      {data && !data.vault && adding?.preset?.id !== "deepseek" && (
        <p className="callout">
          This server can't store keys: that needs <code>AI_PROXY_VAULT_KEYS</code> set on it. Instead, put each key in an environment variable on the server
          whose name starts with <code>{KEY_ENV_PREFIX}</code> and give that name as the key's environment variable.
        </p>
      )}
      {data && (
        <AddProvider
          presets={data.presets}
          taken={names}
          disabled={adding !== null}
          onPick={(name, fields, preset) => {
            setAdding({ name, fields, preset });
            setOpen(null);
          }}
        />
      )}
      {data && adding && (
        <div className="panel">
          {adding.preset?.id === "deepseek" ? <DeepSeekSetup initialName={adding.name} initial={adding.fields} data={data} org={org} onDone={() => setAdding(null)} /> : <>
          <div className="panel-head">
            <h3>New provider{adding.preset ? `: ${adding.preset.label}` : ""}</h3>
            {adding.preset?.notes && <p className="subtle">{adding.preset.notes}</p>}
          </div>
          <ProviderEditor
            key="new"
            initialName={adding.name}
            initial={adding.fields}
            data={data}
            org={org}
            onDone={(saved) => {
              setAdding(null);
              if (saved) setOpen(saved);
            }}
          />
          </>}
        </div>
      )}
      {data && providers.length === 0 && !adding && <p className="empty panel">{org.name} has no API providers yet. Add one from a preset, or start from a blank one.</p>}
      {data &&
        CATEGORY_ORDER.map((category) => {
          const items = providers.filter((item) => item.category === category);
          if (!items.length) return null;
          return (
            <div className="api-provider-group" key={category}>
              <div className="panel-head">
                <h3>{CATEGORY_LABELS[category].title}</h3>
                <p className="subtle">{CATEGORY_LABELS[category].summary}</p>
              </div>
              <ul className="stored-agents">
                {items.map((provider) => (
                  <ProviderCard
                    key={provider.name}
                    provider={provider}
                    data={data}
                    org={org}
                    open={open === provider.name}
                    onToggle={() => setOpen(open === provider.name ? null : provider.name)}
                  />
                ))}
              </ul>
            </div>
          );
        })}
    </section>
  );
}

/** Pick a preset (or a blank provider) and a name, then fill in the rest in the editor. */
function AddProvider({
  presets,
  taken,
  disabled,
  onPick,
}: {
  presets: ApiPreset[];
  taken: string[];
  disabled: boolean;
  onPick: (name: string, fields: ApiProviderFields, preset: ApiPreset | null) => void;
}) {
  const [choice, setChoice] = useState("");
  const preset = presets.find((item) => item.id === choice) ?? null;
  const deepseek = presets.find((item) => item.id === "deepseek");
  return (
    <div className="api-add-options">
      {deepseek && <button type="button" className="deepseek-add" disabled={disabled} onClick={() => onPick(freeName(deepseek.id, taken), fromPreset(deepseek), deepseek)}>
        <span className="deepseek-mark" aria-hidden="true">D</span>
        <span><strong>DeepSeek</strong><small>Connect your API key. Models and Claude Code settings are handled for you.</small></span>
        <span className="deepseek-add-action">Connect DeepSeek</span>
      </button>}
    <form
      className="row wrap api-add"
      onSubmit={(event) => {
        event.preventDefault();
        onPick(freeName(preset?.id ?? "api", taken), preset ? fromPreset(preset) : BLANK, preset);
      }}
    >
      <label className="field">
        Start from
        <select value={choice} onChange={(event) => setChoice(event.target.value)} disabled={disabled}>
          <option value="">A blank provider</option>
          {presets.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={disabled}>
        <Icon name="plus" size={16} />
        Add a provider
      </button>
    </form>
    </div>
  );
}

function ProviderCard({ provider, data, org, open, onToggle }: { provider: ApiProvider; data: ApiProvidersResponse; org: Org; open: boolean; onToggle: () => void }) {
  const client = useQueryClient();
  const machines = useMachines();
  const [removing, setRemoving] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const deepseek = Boolean(provider.balance_supported);
  const [test, setTest] = useState<ApiProviderTest | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries();
  const toggle = useMutation({
    mutationFn: () => api.saveApiProvider(org.id, provider.name, { ...fieldsOf(provider), enabled: !provider.enabled }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteApiProvider(org.id, provider.name),
    onSuccess: () => {
      setRemoving(false);
      refresh();
    },
  });
  const check = useMutation({
    mutationFn: () => api.testApiProvider(org.id, provider.name),
    onMutate: () => {
      setTest(null);
      setTestError(null);
    },
    onSuccess: (result) => {
      setTest(result);
      client.invalidateQueries({ queryKey: ["api-providers", org.id] });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error: Error) => setTestError(error.message),
  });
  const balance = useMutation({ mutationFn: () => api.apiProviderBalance(org.id, provider.name) });
  const health = healthText(provider.health);
  const aliases = Object.entries(provider.models);
  const machineName = provider.machine ? machines.data?.machines.find((item) => item.id === provider.machine)?.name ?? provider.machine : null;
  return (
    <li className={`panel ${provider.enabled ? "" : "is-off"}`}>
      <div className="row wrap stored-agent-head">
        <StateDot tone={provider.enabled ? health.tone : "idle"} />
        <strong>{deepseek ? "DeepSeek" : provider.name}</strong>
        {deepseek && <span className="pill mono">{provider.name}</span>}
        {provider.default && <span className="pill accent">Default</span>}
        {!provider.enabled && <span className="pill">Off</span>}
        {machineName && <span className="pill">On {machineName}</span>}
        <span className="spacer" />
        {provider.balance_supported && <button type="button" onClick={() => balance.mutate()} disabled={balance.isPending || !provider.has_key}
          title={provider.has_key ? "Check the balance using this provider's key" : "Set an API key first"}>
          {balance.isPending ? "Checking balance…" : "Check balance"}
        </button>}
        <button type="button" onClick={() => check.mutate()} disabled={check.isPending}>
          {check.isPending ? "Refreshing…" : deepseek ? "Refresh models" : "Test connection"}
        </button>
        <button type="button" onClick={() => { setAdvanced(false); onToggle(); }} aria-expanded={open}>
          {open ? "Close" : deepseek ? "DeepSeek settings" : "Edit"}
        </button>
      </div>
      <dl className="api-provider-facts">
        {!deepseek && <><dt>Address</dt>
        <dd className="mono">{provider.url}{provider.anthropic_url && provider.anthropic_url !== provider.url && <> · Anthropic at {provider.anthropic_url}</>}</dd>
        <dt>Speaks</dt>
        <dd className="chips">
          {provider.protocols.map((protocol) => (
            <span key={protocol} className="pill">{protocolLabel(protocol)}</span>
          ))}
        </dd>
        </>}
        <dt>{deepseek ? "API key" : "Sign-in"}</dt>
        <dd>
          {!deepseek && <>{AUTH_LABELS[provider.auth] ?? provider.auth} · </>}{keyText(provider)}
        </dd>
        <dt>Models</dt>
        <dd>
          {aliases.length ? (
            <span className="chips">
              {aliases.map(([alias, id]) => (
                <span key={alias} className="pill mono" title={alias === id ? id : `${alias} is ${id}`}>
                  {alias === id ? alias : `${alias} → ${id}`}
                </span>
              ))}
            </span>
          ) : (
            !provider.discovered_models?.length && <span className="subtle">{deepseek ? "Waiting for DeepSeek's model list" : "None named"}</span>
          )}
          {provider.discover && (provider.discovered_models?.length ? (
            <span className="chips" aria-label="Discovered models">
              {provider.discovered_models.filter((id) => !(id in provider.models)).map((id) => <span key={id} className="pill mono">{id}</span>)}
            </span>
          ) : <span className="subtle">{provider.auth === "agent" ? " · automatic discovery needs server-side authentication" : " · models are fetched automatically when a server key is available"}</span>)}
        </dd>
        {!deepseek && <><dt>Priority</dt>
        <dd>
          {provider.priority}
          {provider.fallback.length > 0 && <> · then {provider.fallback.join(", ")} when it can't be reached</>}
        </dd>
        </>}
        <dt>{deepseek ? "Access" : "Who"}</dt>
        <dd>{audience(provider, org)}</dd>
        <dt>{deepseek ? "Connection" : "Health"}</dt>
        <dd className={health.tone === "bad" ? "bad" : undefined}>{health.text}</dd>
      </dl>
      {testError && <p className="bad" role="status">{testError}</p>}
      {balance.isError && <p className="bad" role="alert">{balance.error.message}</p>}
      {balance.data && <div className="callout" role="status">
        <strong>{balance.data.is_available ? "Balance available for API calls" : "Insufficient balance for API calls"}</strong>
        <div className="table-scroll"><table className="table compact">
          <thead><tr><th scope="col">Currency</th><th scope="col" className="num">Total balance</th><th scope="col" className="num">Granted balance</th><th scope="col" className="num">Topped-up balance</th></tr></thead>
          <tbody>{balance.data.balance_infos.map((item) => <tr key={item.currency}><td>{item.currency}</td><td className="num">{item.total_balance}</td><td className="num">{item.granted_balance}</td><td className="num">{item.topped_up_balance}</td></tr>)}</tbody>
        </table></div>
        {!balance.data.balance_infos.length && <p>No currency balances were returned.</p>}
      </div>}
      {test && <TestResult provider={provider} org={org} result={test} />}
      {deepseek && !open && <div className="deepseek-next"><strong>Use DeepSeek with Claude Code</strong>
        <p>Start an agent, choose Claude Code and select <span className="mono">{provider.name}</span> as the provider. Pick a model from the list above; its context settings are applied automatically.</p>
        <Link to="/agents?agent=claude">Start Claude Code</Link>
      </div>}
      {open && deepseek && !advanced ? <DeepSeekSetup provider={provider} initialName={provider.name} initial={fieldsOf(provider)} data={data} org={org} onDone={() => onToggle()} /> : open && (
        <ProviderEditor
          key={provider.name}
          provider={provider}
          initialName={provider.name}
          initial={fieldsOf(provider)}
          data={data}
          org={org}
          onDone={() => onToggle()}
        />
      )}
      <div className="row wrap">
        <button type="button" onClick={() => toggle.mutate()} disabled={toggle.isPending}>
          {provider.enabled ? "Turn off" : "Turn on"}
        </button>
        <span className="spacer" />
        {deepseek && !open && <button type="button" onClick={() => { setAdvanced(true); onToggle(); }}>Advanced settings</button>}
        {toggle.isError && <span className="bad">{toggle.error.message}</span>}
        <button
          type="button"
          className="danger"
          disabled={provider.default}
          title={provider.default ? "It's the default backend: choose another default first" : undefined}
          onClick={() => setRemoving(true)}
        >
          Remove
        </button>
      </div>
      <ConfirmDialog
        open={removing}
        title={`Remove ${provider.name}?`}
        confirmLabel="Remove"
        busy={remove.isPending}
        error={remove.error?.message}
        onConfirm={() => remove.mutate()}
        onCancel={() => setRemoving(false)}
      >
        Agents can't use it any more, and its stored key is deleted. {provider.machine ? "Its machine keeps running the server; you can add it again from the machine's page." : ""}
      </ConfirmDialog>
    </li>
  );
}

/** What a connection test found, with the models it lists that aren't named yet. */
function TestResult({ provider, org, result }: { provider: ApiProvider; org: Org; result: ApiProviderTest }) {
  const client = useQueryClient();
  const fresh = (result.models ?? []).filter((id) => !(id in provider.models) && !Object.values(provider.models).includes(id));
  const [chosen, setChosen] = useState<string[]>([]);
  const add = useMutation({
    mutationFn: () =>
      api.saveApiProvider(org.id, provider.name, {
        ...fieldsOf(provider),
        models: { ...provider.models, ...Object.fromEntries(chosen.map((id) => [id, id])) },
      }),
    onSuccess: () => {
      setChosen([]);
      client.invalidateQueries();
    },
  });
  const toggle = (id: string) => setChosen((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  return (
    <div className="callout" role="status">
      <p className={result.ok ? "good" : "bad"}>
        {result.ok ? "It answered" : "It didn't answer"}
        {result.status != null && ` (${result.status})`}
        {result.latency_ms != null && ` in ${result.latency_ms} ms`}
        {result.error && `: ${result.error}`}
      </p>
      {result.models && (
        <p className="subtle">
          It lists {result.models.length} {result.models.length === 1 ? "model" : "models"}
          {fresh.length ? `, ${fresh.length} not named here yet.` : ", all named here already."}
        </p>
      )}
      {fresh.length > 0 && (
        <>
          <div className="api-discovered">
            {fresh.map((id) => (
              <label key={id} className="check">
                <input type="checkbox" checked={chosen.includes(id)} onChange={() => toggle(id)} />
                <span className="mono">{id}</span>
              </label>
            ))}
          </div>
          <div className="row wrap">
            <button type="button" className="small" onClick={() => setChosen(chosen.length === fresh.length ? [] : fresh)}>
              {chosen.length === fresh.length ? "Choose none" : "Choose all"}
            </button>
            <button type="button" className="primary small" disabled={!chosen.length || add.isPending} onClick={() => add.mutate()}>
              {add.isPending ? "Adding…" : `Add ${chosen.length || ""} ${chosen.length === 1 ? "model" : "models"}`}
            </button>
            {add.isError && <span className="bad">{add.error.message}</span>}
          </div>
        </>
      )}
    </div>
  );
}

interface Pair {
  key: number;
  name: string;
  value: string;
}

let nextKey = 0;
const pairsOf = (record: Record<string, string>): Pair[] => Object.entries(record).map(([name, value]) => ({ key: nextKey++, name, value }));
const recordOf = (pairs: Pair[]): Record<string, string> =>
  Object.fromEntries(pairs.filter((pair) => pair.name.trim()).map((pair) => [pair.name.trim(), pair.value.trim() || pair.name.trim()]));

/** Rows of two text fields, such as a model's alias and its id, or a header's name and value. */
function PairsEditor({
  pairs,
  onChange,
  labels,
  placeholders,
  addLabel,
  mono,
}: {
  pairs: Pair[];
  onChange: (pairs: Pair[]) => void;
  labels: [string, string];
  placeholders: [string, string];
  addLabel: string;
  mono?: boolean;
}) {
  const update = (key: number, patch: Partial<Pair>) => onChange(pairs.map((pair) => (pair.key === key ? { ...pair, ...patch } : pair)));
  return (
    <div className="api-pairs">
      {pairs.map((pair) => (
        <div key={pair.key} className="api-pair">
          <input className={mono ? "mono" : undefined} aria-label={labels[0]} placeholder={placeholders[0]} value={pair.name} onChange={(event) => update(pair.key, { name: event.target.value })} autoCapitalize="none" />
          <input className={mono ? "mono" : undefined} aria-label={labels[1]} placeholder={placeholders[1]} value={pair.value} onChange={(event) => update(pair.key, { value: event.target.value })} autoCapitalize="none" />
          <button type="button" className="icon-button danger" title="Remove" aria-label={`Remove ${pair.name || "this row"}`} onClick={() => onChange(pairs.filter((item) => item.key !== pair.key))}>
            <Icon name="trash" />
          </button>
        </div>
      ))}
      <button type="button" className="small" onClick={() => onChange([...pairs, { key: nextKey++, name: "", value: "" }])}>
        <Icon name="plus" size={14} />
        {addLabel}
      </button>
    </div>
  );
}

/** Add or change one provider. The key field is write-only: empty keeps the stored key. */
function ProviderEditor({
  provider,
  initialName,
  initial,
  data,
  org,
  onDone,
}: {
  provider?: ApiProvider;
  initialName: string;
  initial: ApiProviderFields;
  data: ApiProvidersResponse;
  org: Org;
  onDone: (saved: string | null) => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(initialName);
  const [fields, setFields] = useState<ApiProviderFields>(initial);
  const [models, setModels] = useState<Pair[]>(() => pairsOf(initial.models));
  const [headers, setHeaders] = useState<Pair[]>(() => pairsOf(initial.headers));
  const [key, setKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [orgsText, setOrgsText] = useState(initial.orgs.filter((org) => org !== "*").join(", "));
  const [everyOrg, setEveryOrg] = useState(initial.orgs.includes("*"));
  const set = (patch: Partial<ApiProviderFields>) => setFields((current) => ({ ...current, ...patch }));
  const onMachine = Boolean(provider?.machine);
  const others = useMemo(() => data.providers.filter((item) => item.name !== (provider?.name ?? name.trim())), [data.providers, provider, name]);
  const needsKey = fields.auth !== "none" && fields.auth !== "agent";
  const protocols = data.protocols.length ? data.protocols : (["openai-chat", "openai-responses", "anthropic"] as ApiProtocol[]);
  const authModes = data.auth_modes.length ? data.auth_modes : (Object.keys(AUTH_LABELS) as ApiAuthMode[]);

  const save = useMutation({
    mutationFn: (body: ApiProviderSave) => api.saveApiProvider(org.id, name.trim(), body),
    onSuccess: (saved) => {
      client.invalidateQueries();
      onDone(saved.name);
    },
  });
  const [problem, setProblem] = useState<string | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setProblem(null);
    if (!fields.protocols.length) return setProblem("Choose at least one API it speaks.");
    const keyEnv = fields.key_env?.trim() || null;
    if (keyEnv && !keyEnv.startsWith(KEY_ENV_PREFIX)) return setProblem(`The key's environment variable must start with ${KEY_ENV_PREFIX}.`);
    const body: ApiProviderSave = {
      ...fields,
      url: fields.url.trim(),
      anthropic_url: fields.anthropic_url?.trim() || null,
      api_version: fields.api_version?.trim() || null,
      key_env: keyEnv,
      models: recordOf(models),
      headers: recordOf(headers),
      teams: fields.teams.filter((key) => org.teams.some((team) => team.key === key)),
      orgs: everyOrg ? ["*"] : orgsText.split(",").map((item) => item.trim()).filter(Boolean),
      fallback: fields.fallback.filter((item) => others.some((other) => other.name === item)),
    };
    if (key.trim()) body.key = key.trim();
    else if (clearKey) body.clear_key = true;
    save.mutate(body);
  };

  return (
    <form className="stored-editor api-editor" onSubmit={submit}>
      <div className="form-grid">
        <label className="field">
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} required pattern="[A-Za-z0-9_\-]+" disabled={Boolean(provider)} autoCapitalize="none" />
          <small>Letters, numbers, _ and -. Agents choose it with --backend.</small>
        </label>
        <label className="field">
          Kind
          <select value={fields.category} onChange={(event) => set({ category: event.target.value as ApiCategory })} disabled={onMachine}>
            {(onMachine || initial.category === "machine") && <option value="machine">On a machine</option>}
            <option value="hosted">Hosted API</option>
            <option value="gateway">Gateway</option>
          </select>
        </label>
        <label className="field span-2">
          Address
          <input type="url" value={fields.url} placeholder="https://api.example.com/v1" onChange={(event) => set({ url: event.target.value })} required disabled={onMachine} />
          <small>{onMachine ? "Its machine reports where it is." : "Its OpenAI-compatible base, usually ending in /v1."}</small>
        </label>
        {fields.protocols.includes("anthropic") && (
          <label className="field span-2">
            Anthropic address
            <input type="url" value={fields.anthropic_url ?? ""} placeholder="The same as the address" onChange={(event) => set({ anthropic_url: event.target.value })} />
            <small>Where its Anthropic Messages API is, when that's somewhere else.</small>
          </label>
        )}
        <fieldset className="field span-2">
          <legend>Speaks</legend>
          <span className="row wrap">
            {protocols.map((protocol) => (
              <label key={protocol} className="check">
                <input
                  type="checkbox"
                  checked={fields.protocols.includes(protocol)}
                  onChange={() =>
                    set({ protocols: fields.protocols.includes(protocol) ? fields.protocols.filter((item) => item !== protocol) : [...fields.protocols, protocol] })
                  }
                />
                {protocolLabel(protocol)}
              </label>
            ))}
          </span>
          <small>Agents that speak one of these can use it.</small>
        </fieldset>
        <label className="field">
          Sign-in
          <select value={fields.auth} onChange={(event) => set({ auth: event.target.value as ApiAuthMode })}>
            {authModes.map((mode) => (
              <option key={mode} value={mode}>{AUTH_LABELS[mode] ?? mode}</option>
            ))}
          </select>
          <small>{fields.auth === "agent" ? "Each agent's own key is passed on; the server keeps none." : "How the server sends the key."}</small>
        </label>
        {needsKey && (
          <label className="field">
            Key
            <input
              type="password"
              value={key}
              autoComplete="new-password"
              placeholder={provider?.has_key && !clearKey ? "Saved — enter a new key to replace it" : data.vault ? "Paste the key" : "Needs AI_PROXY_VAULT_KEYS on the server"}
              onChange={(event) => setKey(event.target.value)}
              disabled={!data.vault}
            />
            <small>
              {clearKey ? "The stored key is removed when you save. " : ""}
              {provider?.has_key && provider.key_source !== "env" && !key && (
                <button type="button" className="link-button" onClick={() => setClearKey(!clearKey)}>
                  {clearKey ? "Keep the key" : "Remove key"}
                </button>
              )}
              {!data.vault && "Use an environment variable instead."}
            </small>
          </label>
        )}
        {needsKey && (
          <label className="field span-2">
            Key's environment variable
            <input
              className="mono"
              value={fields.key_env ?? ""}
              placeholder={`${KEY_ENV_PREFIX}${(name || "NAME").toUpperCase().replace(/[^A-Z0-9]+/g, "_")}`}
              onChange={(event) => set({ key_env: event.target.value })}
              pattern={`${KEY_ENV_PREFIX}[A-Za-z0-9_]+`}
              autoCapitalize="none"
            />
            <small>
              Optional. A variable set on the server holding the key; its name must start with <code>{KEY_ENV_PREFIX}</code>. A stored key comes first.
            </small>
          </label>
        )}
        <label className="field">
          API version
          <input value={fields.api_version ?? ""} placeholder="None" onChange={(event) => set({ api_version: event.target.value })} autoCapitalize="none" />
          <small>Azure OpenAI's api-version. Leave empty for others.</small>
        </label>
        <label className="field">
          Connection timeout
          <input type="number" min={1} max={300} value={fields.connect_timeout} onChange={(event) => set({ connect_timeout: Number(event.target.value) })} required />
          <small>Seconds to wait for it to accept a connection.</small>
        </label>
      </div>

      <fieldset className="field">
        <legend>Models</legend>
        <small>The name agents ask for, and the provider's own id for it. Leave the id empty when it's the same.</small>
        <PairsEditor pairs={models} onChange={setModels} labels={["Name agents use", "Provider's model id"]} placeholders={["coder", "provider/model-id"]} addLabel="Add a model" mono />
        <label className="check">
          <input type="checkbox" checked={fields.discover} onChange={(event) => set({ discover: event.target.checked })} />
          Also offer the models it lists itself
        </label>
        <small>Hosted providers are checked automatically. Lists refresh after five minutes; Test connection refreshes now. Your aliases stay alongside discovered models.</small>
      </fieldset>

      <fieldset className="field">
        <legend>Extra headers</legend>
        <small>Sent with every request, such as HTTP-Referer and X-Title for OpenRouter. Not for keys: those go above.</small>
        <PairsEditor pairs={headers} onChange={setHeaders} labels={["Header", "Value"]} placeholders={["X-Title", "Agent mesh"]} addLabel="Add a header" />
      </fieldset>

      <div className="form-grid">
        <label className="field">
          Priority
          <input type="number" value={fields.priority} onChange={(event) => set({ priority: Number(event.target.value) })} required />
          <small>Higher is preferred when the server picks a provider by itself.</small>
        </label>
        <fieldset className="field">
          <legend>Teams that may use it</legend>
          {org.teams.length ? (
            <span className="row wrap">
              {org.teams.map((team) => (
                <label key={team.key} className="check">
                  <input
                    type="checkbox"
                    checked={fields.teams.includes(team.key)}
                    onChange={() => set({ teams: fields.teams.includes(team.key) ? fields.teams.filter((item) => item !== team.key) : [...fields.teams, team.key] })}
                  />
                  {team.name}
                </label>
              ))}
            </span>
          ) : (
            <span className="subtle">{org.root ? "Site admins can always use it." : `${org.name} has no teams yet.`}</span>
          )}
          <small>{fields.teams.length ? `Only people in these teams, and ${org.name}'s admins.` : `None chosen: everyone in ${org.name}.`}</small>
        </fieldset>
        {data.share && (
          <fieldset className="field">
            <legend>Also share with</legend>
            <label className="check">
              <input type="checkbox" checked={everyOrg} onChange={(event) => setEveryOrg(event.target.checked)} />
              Every organization
            </label>
            {!everyOrg && (
              <input value={orgsText} onChange={(event) => setOrgsText(event.target.value)} placeholder="No other organization" aria-label="Organization ids" autoCapitalize="none" />
            )}
            <small>{everyOrg ? "Everyone in any organization can use it, with this key." : "Other organizations' ids, separated by commas. Everyone in them can use it, with this key."}</small>
          </fieldset>
        )}
        <fieldset className="field span-2">
          <legend>When it can't be reached, try</legend>
          {others.length ? (
            <span className="row wrap">
              {others.map((other) => (
                <label key={other.name} className="check">
                  <input
                    type="checkbox"
                    checked={fields.fallback.includes(other.name)}
                    onChange={() =>
                      set({ fallback: fields.fallback.includes(other.name) ? fields.fallback.filter((item) => item !== other.name) : [...fields.fallback, other.name] })
                    }
                  />
                  {other.name}
                </label>
              ))}
            </span>
          ) : (
            <span className="subtle">{org.name} has no other API providers yet.</span>
          )}
          <small>In the order chosen{fields.fallback.length ? `: ${fields.fallback.join(", ")}` : ""}. Only when it can't be reached at all, not when it answers with an error.</small>
        </fieldset>
        <label className="check">
          <input type="checkbox" checked={fields.verify_tls} onChange={(event) => set({ verify_tls: event.target.checked })} />
          Check its TLS certificate
        </label>
        <label className="check">
          <input type="checkbox" checked={fields.enabled} onChange={(event) => set({ enabled: event.target.checked })} />
          Agents can use it
        </label>
      </div>
      <div className="row wrap">
        <button className="primary" type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : provider ? "Save changes" : "Add provider"}
        </button>
        <button type="button" onClick={() => onDone(null)} disabled={save.isPending}>
          Cancel
        </button>
        {(problem || save.isError) && <span className="bad" role="status">{problem ?? save.error?.message}</span>}
      </div>
    </form>
  );
}

/**
 * An OpenAI-compatible server on a machine (LM Studio, vLLM, SGLang) in its machine's Providers
 * tab: what it serves, read-only, since it's managed in that server itself.
 */
export function ApiServerProvider({ provider }: { provider: MachineProvider }) {
  const models = useModels();
  const identity = useAuth().data?.identity;
  const listing = provider.backend ? models.data?.backends[provider.backend] : undefined;
  const flavor = provider.flavor ? API_FLAVORS[provider.flavor] ?? provider.flavor : "This server";
  const names = (listing?.models ?? []).map((item) => item.name).filter(Boolean);
  return (
    <section className="section" aria-labelledby={`api-models-${provider.id}`}>
      <div className="section-head">
        <h2 id={`api-models-${provider.id}`}>Models</h2>
        <p className="subtle">
          {flavor} speaks the OpenAI API{provider.models != null && `, and serves ${provider.models} ${provider.models === 1 ? "model" : "models"}`}.
        </p>
      </div>
      <p className="subtle">
        Its models are managed in {flavor === "This server" ? "the server" : flavor} itself: load or download them there, and they show up here.
        {identity?.role === "admin" && (
          <>
            {" "}Names agents use for them, who may use it, its priority and fallbacks are in{" "}
            {identity.site_admin ? (
              <>its organization's <Link to="/organization">API providers</Link></>
            ) : (
              <Link to={`/organization?org=${encodeURIComponent(identity.tenant)}&orgTab=providers`}>your organization's API providers</Link>
            )}
            .
          </>
        )}
      </p>
      {!provider.backend ? (
        <p className="empty">Add it as a provider to list its models here.</p>
      ) : listing && !listing.online ? (
        <p className="bad">{listing.error ?? "It isn't answering now."}</p>
      ) : names.length ? (
        <ul className="provider-backends">
          {names.map((name) => (
            <li key={name} className="api-model">
              <span className="mono">{name}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty">{models.isPending ? "Reading its models…" : "It lists no models now."}</p>
      )}
    </section>
  );
}
