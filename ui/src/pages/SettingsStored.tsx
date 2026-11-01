import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  RETENTION_KINDS,
  TRACE_CAPTURES,
  type AuditExport,
  type AuditExportKind,
  type CapturePolicy,
  type RetentionDays,
  type RetentionKind,
  type ServerRetention,
  type StoredAgent,
  type TraceCapture,
} from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Icon } from "../components/Icon";
import { formatBytes, formatNumber } from "../format";
import { useClipboard } from "../hooks";
import { daysText, KIND_LABELS, parseDays, TRACE_LABELS } from "../retention";

/** The Server settings page's sections for what used to be config files: storage, the audit log, agents and model tuning (site admins). */

const NEW_AGENT = `name: My agent
executable: my-agent
protocol: openai-chat
env:
  OPENAI_BASE_URL: "{openai_url}"
  OPENAI_MODEL: "{model}"
args: [--model, "{model}"]
`;

function forbidden(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

type DaysDraft = Record<RetentionKind, string>;
interface PolicyDraft {
  capture: CapturePolicy;
  allowed: TraceCapture[];
  days: DaysDraft;
  max: DaysDraft;
  min: DaysDraft;
}

const toDraft = (policy: ServerRetention): PolicyDraft => {
  const texts = (days: RetentionDays) => Object.fromEntries(RETENTION_KINDS.map((kind) => [kind, daysText(days[kind])])) as DaysDraft;
  return { capture: { ...policy.capture }, allowed: [...policy.allowed_traces], days: texts(policy.days), max: texts(policy.max), min: texts(policy.min) };
};

/** The draft as the server takes it, or the first field that isn't a number of days. */
function fromDraft(draft: PolicyDraft): ServerRetention | string {
  const parts = { days: {}, max: {}, min: {} } as Record<"days" | "max" | "min", RetentionDays>;
  for (const part of ["days", "max", "min"] as const) {
    for (const kind of RETENTION_KINDS) {
      const value = parseDays(draft[part][kind]);
      if (value === undefined) return `${KIND_LABELS[kind].label}: a whole number of days, or empty.`;
      parts[part][kind] = value;
    }
  }
  return { capture: draft.capture, allowed_traces: TRACE_CAPTURES.filter((mode) => draft.allowed.includes(mode)), ...parts };
}

/** Where everything is kept, what's recorded, and how long each kind of recording stays (site admins). */
export function StorageSettings() {
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storage });
  const policy = useQuery({ queryKey: ["retention"], queryFn: api.retentionPolicy });
  const client = useQueryClient();
  const [draft, setDraft] = useState<PolicyDraft | null>(null);
  const [message, setMessage] = useState<{ text: string; tone: "good" | "bad" } | null>(null);
  useEffect(() => {
    if (policy.data) setDraft(toDraft(policy.data));
  }, [policy.data]);
  const save = useMutation({
    mutationFn: (value: ServerRetention) => api.saveRetentionPolicy(value),
    onSuccess: (result) => {
      client.setQueryData(["retention"], result);
      // The older single setting follows the traces' days; organizations' effective policies follow the server's.
      void client.invalidateQueries({ queryKey: ["storage"] });
      void client.invalidateQueries({ queryKey: ["org-retention"] });
      setMessage({ text: "Saved. Sessions started from now on use it; each one keeps the policy it started under.", tone: "good" });
    },
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  if (forbidden(storage.error) || forbidden(policy.error)) return null;
  const database = storage.data?.database;
  const setDays = (part: "days" | "max" | "min", kind: RetentionKind, text: string) =>
    setDraft((current) => (current ? { ...current, [part]: { ...current[part], [kind]: text } } : current));
  const setCapture = (patch: Partial<CapturePolicy>) => setDraft((current) => (current ? { ...current, capture: { ...current.capture, ...patch } } : current));
  const toggleAllowed = (mode: TraceCapture) =>
    setDraft((current) => (current ? { ...current, allowed: current.allowed.includes(mode) ? current.allowed.filter((item) => item !== mode) : [...current.allowed, mode] } : current));
  return (
    <section className="section" aria-labelledby="storage-heading">
      <div className="section-head">
        <h2 id="storage-heading">Storage</h2>
      </div>
      <div className="panel">
        {storage.isError && <p className="bad">{storage.error.message}</p>}
        {database && (
          <p>
            Everything the server keeps (these settings, accounts, machines, and every session's requests, payloads and terminals)
            is in its {database.kind === "postgresql" ? "PostgreSQL" : "SQLite"} database, <code className="mono wrap">{database.url}</code>
            {database.size != null && <>, {formatBytes(database.size)}</>}.
          </p>
        )}
      </div>
      {policy.isError && <p className="bad">{policy.error.message}</p>}
      {draft && (
        <form
          className="panel retention-form"
          onSubmit={(event) => {
            event.preventDefault();
            setMessage(null);
            const value = fromDraft(draft);
            if (typeof value === "string") setMessage({ text: value, tone: "bad" });
            else save.mutate(value);
          }}
        >
          <div className="panel-head">
            <h3>What's recorded</h3>
            <p className="subtle">What isn't recorded is never written, so there's nothing to delete later. Organizations may change these within what you allow.</p>
          </div>
          <div className="form-grid">
            <label className="field">
              Record traces
              <select value={draft.capture.traces} onChange={(event) => setCapture({ traces: event.target.value as TraceCapture })}>
                {TRACE_CAPTURES.filter((mode) => draft.allowed.includes(mode)).map((mode) => (
                  <option key={mode} value={mode}>{TRACE_LABELS[mode]}</option>
                ))}
              </select>
              <small>Unless an organization chooses otherwise.</small>
            </label>
            <fieldset className="field">
              <legend>Organizations may choose</legend>
              <span className="row wrap">
                {TRACE_CAPTURES.map((mode) => (
                  <label key={mode} className="check">
                    <input type="checkbox" checked={draft.allowed.includes(mode)} onChange={() => toggleAllowed(mode)} />
                    {TRACE_LABELS[mode]}
                  </label>
                ))}
              </span>
            </fieldset>
            <label className="check span-2">
              <input type="checkbox" checked={draft.capture.intercept_bodies} onChange={(event) => setCapture({ intercept_bodies: event.target.checked })} />
              Put account agents' traffic in their traces
            </label>
            <label className="check span-2">
              <input type="checkbox" checked={draft.capture.terminal_output} onChange={(event) => setCapture({ terminal_output: event.target.checked })} />
              Record shared terminals' output
            </label>
          </div>
          <div className="panel-head">
            <h3>How long it's kept</h3>
            <p className="subtle">
              Days since a session's last activity; empty is forever. The bounds are what organizations' admins may set their own within (empty: no
              bound). Deleting runs every hour, and a legal hold stops it for what the hold covers.
            </p>
          </div>
          <div className="retention-kinds" role="table" aria-label="Days each kind is kept">
            <div className="retention-kind retention-kind-head" role="row">
              <span role="columnheader">Kind</span>
              <span role="columnheader">Keep for</span>
              <span role="columnheader">Organizations at least</span>
              <span role="columnheader">Organizations at most</span>
            </div>
            {RETENTION_KINDS.map((kind) => (
              <div key={kind} className="retention-kind" role="row">
                <span role="rowheader">
                  <strong>{KIND_LABELS[kind].label}</strong>
                  <small className="subtle">{KIND_LABELS[kind].hint}</small>
                </span>
                {(["days", "min", "max"] as const).map((part) => (
                  <label key={part} className="field" role="cell">
                    <span className="retention-cell-label">{part === "days" ? "Keep for" : part === "min" ? "At least" : "At most"}</span>
                    <input
                      inputMode="numeric"
                      value={draft[part][kind]}
                      placeholder={part === "days" ? "Forever" : "No bound"}
                      aria-label={`${KIND_LABELS[kind].label}: ${part === "days" ? "keep for, days" : part === "min" ? "organizations at least, days" : "organizations at most, days"}`}
                      aria-invalid={parseDays(draft[part][kind]) === undefined}
                      onChange={(event) => setDays(part, kind, event.target.value)}
                    />
                  </label>
                ))}
              </div>
            ))}
          </div>
          <div className="row wrap">
            <button className="primary" type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save policy"}
            </button>
            <button type="button" disabled={save.isPending || !policy.data} onClick={() => policy.data && (setDraft(toDraft(policy.data)), setMessage(null))}>
              Undo changes
            </button>
            {message && (
              <span className={message.tone} role="status">
                {message.text}
              </span>
            )}
          </div>
        </form>
      )}
    </section>
  );
}

const EXPORT_KINDS: { id: AuditExportKind; label: string; placeholder: string }[] = [
  { id: "otlp", label: "OpenTelemetry (OTLP/HTTP)", placeholder: "https://collector.example.com:4318" },
  { id: "syslog", label: "Syslog (RFC 5424)", placeholder: "syslog+tls://siem.example.com:6514" },
  { id: "webhook", label: "Webhook", placeholder: "https://hooks.example.com/audit" },
  { id: "blob", label: "Blob storage", placeholder: "" },
];

interface ExportRow extends Omit<AuditExport, "tenants" | "token_env"> {
  key: number;
  token_env: string;
  /** Organizations, comma-separated; empty is every one. */
  tenants: string;
}

let nextExport = 0;
const toRow = (item: AuditExport): ExportRow => ({ ...item, key: nextExport++, token_env: item.token_env ?? "", tenants: (item.tenants ?? []).join(", ") });
const fromRow = (row: ExportRow): AuditExport => {
  const tenants = row.tenants.split(",").map((item) => item.trim()).filter(Boolean);
  return {
    name: row.name.trim(), kind: row.kind, url: row.url.trim(), token_env: row.token_env.trim() || null,
    tenants: tenants.length ? tenants : null, enabled: row.enabled, prefix: row.prefix,
  };
};

/** The audit log: where it's sent as it grows, how often its chains are signed, and the key that signs them (site admins). */
export function AuditSettings() {
  const config = useQuery({ queryKey: ["audit-config"], queryFn: api.auditConfig });
  const client = useQueryClient();
  const copy = useClipboard();
  const [rows, setRows] = useState<ExportRow[]>([]);
  const [minutes, setMinutes] = useState("60");
  const [removing, setRemoving] = useState<ExportRow | null>(null);
  const [message, setMessage] = useState<{ text: string; tone: "good" | "bad" } | null>(null);
  useEffect(() => {
    if (!config.data) return;
    setRows(config.data.exports.map(toRow));
    setMinutes(String(config.data.checkpoint_minutes));
  }, [config.data]);
  const save = useMutation({
    mutationFn: () => api.saveAuditConfig({ exports: rows.map(fromRow), checkpoint_minutes: Number(minutes) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["audit-config"] });
      setMessage({ text: "Saved.", tone: "good" });
    },
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  if (forbidden(config.error)) return null;
  const update = (key: number, patch: Partial<ExportRow>) => setRows((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  const data = config.data;
  return (
    <section className="section" aria-labelledby="audit-heading">
      <div className="section-head">
        <h2 id="audit-heading">Audit log</h2>
        <Link to="/audit">Open the audit log</Link>
      </div>
      <p className="subtle">
        Every change to the server, its organizations and machines is recorded, chained by hash for each organization, and the chains' heads are
        signed with the server's own key. Exports send the events elsewhere (a SIEM, a log store) as they're recorded.
      </p>
      {config.isError && <p className="bad">{config.error.message}</p>}
      {data && (
        <div className="panel">
          <dl className="fact-list">
            <div>
              <dt>Signing key id</dt>
              <dd className="mono">{data.key_id ?? "–"}</dd>
            </div>
            <div>
              <dt>Not exported yet</dt>
              <dd className={data.backlog ? "warn" : undefined}>{data.backlog == null ? "–" : `${formatNumber(data.backlog)} events`}</dd>
            </div>
          </dl>
          {data.public_key && (
            <>
              <h3>Public key (Ed25519, base64)</h3>
              <p className="subtle">To check the signed checkpoints anywhere else.</p>
              <CodeLine text={data.public_key} onCopy={() => copy(data.public_key ?? "", "Public key copied")} />
            </>
          )}
        </div>
      )}
      {data && (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            setMessage(null);
            const names = rows.map((row) => row.name.trim());
            if (new Set(names).size !== names.length) setMessage({ text: "Each export needs a different name.", tone: "bad" });
            else save.mutate();
          }}
        >
          <div className="section-head">
            <h3>Exports</h3>
            <button type="button" onClick={() => setRows((current) => [...current, { key: nextExport++, name: "", kind: "otlp", url: "", token_env: "", tenants: "", enabled: true, prefix: "audit/export" }])}>
              <Icon name="plus" size={16} />
              Add export
            </button>
          </div>
          {!rows.length && <p className="subtle">No exports: the audit log stays in the server's database only.</p>}
          <ul className="backend-cards">
            {rows.map((row) => {
              const kind = EXPORT_KINDS.find((item) => item.id === row.kind) ?? EXPORT_KINDS[0];
              return (
                <li key={row.key} className="backend-card">
                  <div className="form-grid">
                    <label className="field">
                      Name
                      <input value={row.name} onChange={(event) => update(row.key, { name: event.target.value })} required maxLength={40} pattern="[A-Za-z0-9_\-]+" placeholder="siem" />
                    </label>
                    <label className="field">
                      Kind
                      <select value={row.kind} onChange={(event) => update(row.key, { kind: event.target.value as AuditExportKind })}>
                        {EXPORT_KINDS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                      </select>
                    </label>
                    {row.kind === "blob" ? (
                      <label className="field span-2">
                        Folder in blob storage
                        <input value={row.prefix} onChange={(event) => update(row.key, { prefix: event.target.value })} placeholder="audit/export" maxLength={200} />
                        <small>Written as NDJSON files to the server's blob storage (AI_PROXY_BLOB_URL).</small>
                      </label>
                    ) : (
                      <label className="field span-2">
                        URL
                        <input value={row.url} onChange={(event) => update(row.key, { url: event.target.value })} placeholder={kind.placeholder} required />
                      </label>
                    )}
                    {row.kind !== "blob" && row.kind !== "syslog" && (
                      <label className="field">
                        Token's environment variable
                        <input value={row.token_env} onChange={(event) => update(row.key, { token_env: event.target.value })} placeholder="AUDIT_EXPORT_TOKEN" pattern="[A-Za-z0-9_]+" autoCapitalize="none" />
                        <small>{row.kind === "webhook" ? "Its value signs each delivery (x-mesh-signature)." : "Its value is sent as the bearer token."} Set it on the server; the secret never goes in the settings.</small>
                      </label>
                    )}
                    <label className="field">
                      Organizations
                      <input value={row.tenants} onChange={(event) => update(row.key, { tenants: event.target.value })} placeholder="Every organization" autoCapitalize="none" />
                      <small>Ids, separated by commas.</small>
                    </label>
                  </div>
                  <div className="backend-card-foot">
                    <label className="check">
                      <input type="checkbox" checked={row.enabled} onChange={(event) => update(row.key, { enabled: event.target.checked })} />
                      Sending
                    </label>
                    <span className="spacer" />
                    <button type="button" className="icon-button danger" title="Remove" aria-label={`Remove ${row.name || "this export"}`} onClick={() => setRemoving(row)}>
                      <Icon name="trash" />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
          <div className="form-grid">
            <label className="field">
              Sign the chains every
              <input type="number" min={5} max={1440} value={minutes} onChange={(event) => setMinutes(event.target.value)} required />
              <small>Minutes, from 5 to 1440. Only when something was recorded since the last checkpoint.</small>
            </label>
          </div>
          <div className="row wrap">
            <button className="primary" type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save audit settings"}
            </button>
            {message && (
              <span className={message.tone} role="status">
                {message.text}
              </span>
            )}
          </div>
        </form>
      )}
      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing?.name || "this export"}?`}
        confirmLabel="Remove"
        onConfirm={() => {
          if (removing) setRows((current) => current.filter((row) => row.key !== removing.key));
          setRemoving(null);
        }}
        onCancel={() => setRemoving(null)}
      >
        It stops getting events once you save. What it already received stays there.
      </ConfirmDialog>
    </section>
  );
}

/** Every agent launchers can start: shipped ones follow the server's releases until changed here. */
export function AgentDefinitions() {
  const agents = useQuery({ queryKey: ["stored-agents"], queryFn: api.storedAgents });
  const client = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<StoredAgent | null>(null);
  const refresh = () => {
    client.invalidateQueries({ queryKey: ["stored-agents"] });
    client.invalidateQueries({ queryKey: ["agents"] });
  };
  const remove = useMutation({
    mutationFn: (id: string) => api.deleteAgent(id),
    onSuccess: () => {
      setRemoving(null);
      refresh();
    },
  });
  if (forbidden(agents.error)) return null;
  return (
    <section className="section" aria-labelledby="agents-heading">
      <div className="section-head">
        <h2 id="agents-heading">Agents</h2>
        <button type="button" onClick={() => setAdding(true)} disabled={adding}>
          Add an agent
        </button>
      </div>
      <p className="subtle">
        How each coding agent is pointed at a session: its command, environment, files and protocol. Placeholders such as{" "}
        <code>{"{openai_url}"}</code> and <code>{"{model}"}</code> are filled in for every launch; the Agents page of the docs lists them.
        Agents that ship with the server follow its updates until you change them; Reset brings one back.
      </p>
      {agents.isError && <p className="bad">{agents.error.message}</p>}
      {adding && <AgentEditor key="new" onDone={() => { setAdding(false); refresh(); }} />}
      <ul className="stored-agents">
        {agents.data?.map((agent) => (
          <li key={agent.id} className="panel">
            <div className="row wrap stored-agent-head">
              <strong>{String(agent.definition.name ?? agent.id)}</strong>
              <code className="mono">{agent.id}</code>
              <span className="pill">{agent.builtin ? (agent.edited ? "Shipped, changed" : "Shipped") : "Added here"}</span>
              <span className="spacer" />
              <button type="button" onClick={() => setOpen(open === agent.id ? null : agent.id)} aria-expanded={open === agent.id}>
                {open === agent.id ? "Close" : "Edit"}
              </button>
              <button type="button" className="danger" onClick={() => setRemoving(agent)}>
                Remove
              </button>
            </div>
            {open === agent.id && <AgentEditor key={agent.id} agent={agent} onDone={() => { setOpen(null); refresh(); }} />}
          </li>
        ))}
      </ul>
      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing ? String(removing.definition.name ?? removing.id) : ""}?`}
        confirmLabel="Remove"
        busy={remove.isPending}
        error={remove.error?.message}
        onConfirm={() => removing && remove.mutate(removing.id)}
        onCancel={() => setRemoving(null)}
      >
        Launchers stop offering it.{" "}
        {removing?.builtin ? "It ships with the server: adding an agent with the same id brings it back." : "This can't be undone."}
      </ConfirmDialog>
    </section>
  );
}

function AgentEditor({ agent, onDone }: { agent?: StoredAgent; onDone: () => void }) {
  const [id, setId] = useState(agent?.id ?? "");
  const [text, setText] = useState(agent?.yaml ?? NEW_AGENT);
  const save = useMutation({ mutationFn: () => api.saveAgent(id.trim(), text), onSuccess: onDone });
  const reset = useMutation({ mutationFn: () => api.resetAgent(agent?.id ?? ""), onSuccess: onDone });
  return (
    <form
      className="stored-editor"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      {!agent && (
        <label className="field">
          Id
          <input value={id} onChange={(event) => setId(event.target.value)} required pattern="[A-Za-z0-9][A-Za-z0-9._\-]*" placeholder="my-agent" />
          <small>What launchers name it: agent {id || "my-agent"} .</small>
        </label>
      )}
      <label className="field">
        Definition (YAML)
        <textarea rows={Math.min(30, Math.max(8, text.split("\n").length + 1))} value={text} onChange={(event) => setText(event.target.value)} spellCheck={false} />
      </label>
      {(save.error || reset.error) && <p className="bad">{(save.error ?? reset.error)?.message}</p>}
      <div className="row wrap">
        <button className="primary" type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save agent"}
        </button>
        {agent?.builtin && agent.edited && (
          <button type="button" onClick={() => reset.mutate()} disabled={reset.isPending}>
            Reset to the shipped one
          </button>
        )}
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** Ollama's model tuning: defaults, profiles (the first whose match fits the model) and aliases, as one YAML document. */
export function ModelTuningEditor() {
  const tuning = useQuery({ queryKey: ["model-tuning"], queryFn: api.modelTuningYaml });
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "good" | "bad" } | null>(null);
  useEffect(() => {
    if (tuning.data !== undefined) setText(tuning.data);
  }, [tuning.data]);
  const save = useMutation({
    mutationFn: () => api.saveModelTuning(text),
    onSuccess: () => {
      setMessage({ text: "Saved; new requests use it.", tone: "good" });
      client.invalidateQueries({ queryKey: ["model-tuning"] });
      client.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  if (forbidden(tuning.error)) return null;
  return (
    <section className="section" aria-labelledby="tuning-heading">
      <div className="section-head">
        <h2 id="tuning-heading">Ollama model tuning</h2>
      </div>
      <p className="subtle">
        Applied to requests the proxy sends to Ollama, in layers: <code>defaults</code>, then the first of <code>profiles</code> whose{" "}
        <code>match</code> fits the model's name, then the model's entry under <code>aliases</code>. A value an agent sends itself
        wins. Strata and Docker Model Runner take requests as they are.
      </p>
      {tuning.isError && <p className="bad">{tuning.error.message}</p>}
      <form
        className="stored-editor panel"
        onSubmit={(event) => {
          event.preventDefault();
          setMessage(null);
          save.mutate();
        }}
      >
        <textarea
          aria-label="Model tuning (YAML)"
          rows={Math.min(36, Math.max(10, text.split("\n").length + 1))}
          value={text}
          onChange={(event) => setText(event.target.value)}
          spellCheck={false}
        />
        <div className="row wrap">
          <button className="primary" type="submit" disabled={save.isPending || tuning.data === undefined}>
            {save.isPending ? "Saving…" : "Save tuning"}
          </button>
          {message && (
            <span className={message.tone} role="status">
              {message.text}
            </span>
          )}
        </div>
      </form>
    </section>
  );
}
