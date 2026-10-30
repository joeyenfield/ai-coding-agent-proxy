import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type StoredAgent } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { formatBytes } from "../format";

/** The Server settings page's sections for what used to be config files: storage, agents and model tuning (admins). */

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

/** Where everything is kept, and how many days sessions' payloads and terminals stay. */
export function StorageSettings() {
  const storage = useQuery({ queryKey: ["storage"], queryFn: api.storage });
  const client = useQueryClient();
  const [days, setDays] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "good" | "bad" } | null>(null);
  useEffect(() => {
    if (storage.data) setDays(storage.data.retention_days ? String(storage.data.retention_days) : "");
  }, [storage.data]);
  const save = useMutation({
    mutationFn: () => api.saveRetention(days.trim() ? Number(days) : null),
    onSuccess: (result) => {
      client.setQueryData(["storage"], result);
      setMessage({ text: result.retention_days ? `Kept for ${result.retention_days} days.` : "Kept until you delete them.", tone: "good" });
    },
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  if (forbidden(storage.error)) return null;
  const database = storage.data?.database;
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
        <form
          className="form-grid"
          onSubmit={(event) => {
            event.preventDefault();
            setMessage(null);
            save.mutate();
          }}
        >
          <label className="field">
            Keep payloads and terminals for
            <input type="number" min={1} max={36500} placeholder="Forever" value={days} onChange={(event) => setDays(event.target.value)} />
            <small>Days since a session's last request. Its requests and token counts stay; empty keeps everything.</small>
          </label>
          <div className="field">
            <span aria-hidden="true">&nbsp;</span>
            <div className="row wrap">
              <button type="submit" disabled={save.isPending || !storage.data}>
                {save.isPending ? "Saving…" : "Save"}
              </button>
              {message && (
                <span className={message.tone} role="status">
                  {message.text}
                </span>
              )}
            </div>
          </div>
        </form>
      </div>
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
