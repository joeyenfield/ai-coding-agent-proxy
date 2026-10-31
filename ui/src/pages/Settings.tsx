import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiUrl, MACHINE_TYPES, type NetworkConfig } from "../api";
import { Icon } from "../components/Icon";
import { PartTag } from "../components/PartTag";
import { RestartButton } from "../components/Restart";
import { useStatus } from "../hooks";
import { machinePath } from "./Machine";
import { AgentDefinitions, ModelTuningEditor, StorageSettings } from "./SettingsStored";

const URL_PLACEHOLDERS: Record<string, string> = {
  ollama: "http://host:11434",
  docker: "http://localhost:12434",
  strata: "http://127.0.0.1:18500",
  anthropic: "https://api.anthropic.com",
};

/** What each backend type is, in a line under its card. */
const TYPE_HINTS: Record<string, string> = {
  ollama: "Local models; the proxy translates requests and applies the model tuning below.",
  docker: "Docker Model Runner; requests pass through unchanged.",
  strata: "A Strata server; requests pass through with Strata's API key added.",
  anthropic: "Hosted API; requests pass through with the agent's own credentials.",
};

interface Row {
  id: number;
  name: string;
  type: string;
  url: string;
  /** Run in a container by its machine: changed on the Containers page, not here. */
  managed?: boolean;
  /** A model server on a machine: added from the machine, so its type and URL aren't edited here. */
  local?: boolean;
}

let nextId = 0;

export function SettingsPage() {
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const status = useStatus();
  const client = useQueryClient();
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [url, setUrl] = useState("");
  const [publicUrl, setPublicUrl] = useState("");
  const [defaultBackend, setDefaultBackend] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [message, setMessage] = useState<{ text: string; tone: "good" | "warn" | "bad" } | null>(null);

  useEffect(() => {
    if (!config.data) return;
    const value = config.data.config;
    setHost(value.proxy.listen_host);
    setPort(String(value.proxy.listen_port));
    setUrl(value.proxy.url);
    setPublicUrl(value.proxy.public_url ?? "");
    setDefaultBackend(value.default_backend);
    setRows(
      Object.entries(value.backends).map(([name, backend]) => ({
        id: nextId++, name, type: backend.type, url: backend.url, managed: Boolean(backend.container),
        local: MACHINE_TYPES.includes(backend.type),
      })),
    );
  }, [config.data]);

  const save = useMutation({
    mutationFn: api.saveConfig,
    onSuccess: (result) => {
      setMessage(
        result.restart_required
          ? { text: "Saved. Restart the proxy to use the new listen address.", tone: "warn" }
          : { text: "Saved and applied.", tone: "good" },
      );
      client.invalidateQueries();
    },
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });

  const names = rows.map((row) => row.name.trim()).filter(Boolean);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (new Set(names).size !== names.length) {
      setMessage({ text: "Each backend needs a different name.", tone: "bad" });
      return;
    }
    const backends: NetworkConfig["backends"] = {};
    for (const row of rows) {
      backends[row.name.trim()] = { type: row.type, url: row.url.trim() };
    }
    setMessage(null);
    save.mutate({
      proxy: { listen_host: host.trim(), listen_port: Number(port), url: url.trim(), public_url: publicUrl.trim() || null },
      default_backend: names.includes(defaultBackend) ? defaultBackend : (names[0] ?? ""),
      backends,
    });
  };
  const machineOf = (row: Row) => status.data?.backends[row.name]?.machine_name;
  // The machine's page for a backend's model server, where it's managed.
  const pageOf = (row: Row) => {
    const backend = status.data?.backends[row.name];
    return backend?.machine ? machinePath(backend.machine, backend.provider) : "/machines";
  };
  const update = (id: number, patch: Partial<Row>) => setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Server settings</h1>
        <p className="lede">
          The hub's address, which provider agents use by default, the agents themselves and how long recordings are kept. All
          of it is kept in the server's database; changes apply right away. To script the server, see its{" "}
          <a href={apiUrl("/docs")} target="_blank" rel="noreferrer">API reference</a> (<code>/openapi.json</code>).
        </p>
      </header>
      {config.isError && <p className="bad">{config.error.message}</p>}
      <form className="settings-form" onSubmit={submit}>
        <section className="section" aria-labelledby="server-heading">
          <div className="section-head">
            <h2 id="server-heading">Server</h2>
          </div>
          <div className="panel">
            <div className="form-grid">
              <label className="field">
                Listen host
                <input value={host} onChange={(event) => setHost(event.target.value)} required />
                <small>0.0.0.0 accepts other machines. Sign-in is required either way.</small>
              </label>
              <label className="field">
                Listen port
                <input type="number" min={1} max={65535} value={port} onChange={(event) => setPort(event.target.value)} required />
                <small>A change needs a restart.</small>
              </label>
              <label className="field span-2">
                Address agent machines use
                <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} required />
                <small>Shown for <code>agent login</code> and <code>agent-machine login</code>. Use this machine's LAN or public address when sharing it.</small>
              </label>
              <label className="field span-2">
                Public console address
                <input type="url" value={publicUrl} placeholder="https://agents.example.com" onChange={(event) => setPublicUrl(event.target.value)} />
                <small>Where people open the console from anywhere, shown ahead of the addresses the server finds itself. Optional.</small>
              </label>
            </div>
          </div>
        </section>
        <section className="section" aria-labelledby="backends-heading">
          <div className="section-head">
            <h2 id="backends-heading">Providers</h2>
            <button type="button" onClick={() => setRows((current) => [...current, { id: nextId++, name: "", type: "anthropic", url: "https://api.anthropic.com" }])}>
              <Icon name="plus" size={16} />
              Add hosted API
            </button>
          </div>
          <p className="subtle">
            Local model servers come from your machines: add them, and manage their models, on each machine's page under{" "}
            <Link to="/machines">Machines</Link>. Here you pick the default, which agents started without{" "}
            <code>--backend</code> use, and remove ones you no longer need. Agents signed in to their own accounts don't use one.
          </p>
          {rows.length === 0 && <p className="subtle">No providers yet.</p>}
          <ul className="backend-cards">
            {rows.map((row) => (
              <li key={row.id} className={`backend-card ${row.managed ? "managed" : ""} ${defaultBackend === row.name ? "is-default" : ""}`}>
                <div className="backend-card-grid">
                  <label className="field">
                    Name
                    <input value={row.name} onChange={(event) => update(row.id, { name: event.target.value })} required pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" disabled={row.managed || row.local} />
                  </label>
                  <label className="field">
                    Type
                    <select value={row.type} onChange={(event) => update(row.id, { type: event.target.value })} disabled={row.managed || row.local}>
                      {row.local && (
                        <>
                          <option value="ollama">Ollama</option>
                          <option value="docker">Docker Model Runner</option>
                          <option value="strata">Strata</option>
                        </>
                      )}
                      <option value="anthropic">Anthropic API</option>
                    </select>
                  </label>
                  <label className="field url-field">
                    URL
                    <input type="url" value={row.url} placeholder={URL_PLACEHOLDERS[row.type] ?? URL_PLACEHOLDERS.ollama} onChange={(event) => update(row.id, { url: event.target.value })} required disabled={row.managed || row.local} />
                  </label>
                </div>
                <div className="backend-card-foot">
                  <span className="subtle backend-hint">
                    {row.managed ? (
                      <>
                        A container on {machineOf(row) ?? "a machine"}; change or delete it on{" "}
                        <Link to={machinePath(status.data?.backends[row.name]?.machine ?? "", "containers")}>its machine's Containers</Link>.
                      </>
                    ) : row.local ? (
                      machineOf(row) ? (
                        <>On <Link to={pageOf(row)}>{machineOf(row)}</Link>. {TYPE_HINTS[row.type]}</>
                      ) : (
                        <>Not on a machine yet: sign in the machine that runs it. {TYPE_HINTS[row.type]}</>
                      )
                    ) : (
                      TYPE_HINTS[row.type]
                    )}
                  </span>
                  <label className="check">
                    <input type="radio" name="default" checked={defaultBackend === row.name} onChange={() => setDefaultBackend(row.name)} />
                    Default
                  </label>
                  <button
                    type="button"
                    className="icon-button danger"
                    title="Remove"
                    aria-label={`Remove ${row.name || "this backend"}`}
                    disabled={row.managed}
                    onClick={() => setRows((current) => current.filter((item) => item.id !== row.id))}
                  >
                    <Icon name="trash" />
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
        <div className="save-bar floating">
          {message ? (
            <span className={message.tone} role="status">
              {message.text}
            </span>
          ) : (
            <span className="subtle">Saves the server's address and providers.</span>
          )}
          <button className="primary" type="submit" disabled={save.isPending || !config.data}>
            {save.isPending ? "Saving…" : "Save changes"}
          </button>
        </div>
      </form>
      <AgentDefinitions />
      <ModelTuningEditor />
      <StorageSettings />
      {status.data?.restart?.available && (
        <section className="panel" aria-labelledby="restart-heading">
          <div className="panel-head">
            <h2 id="restart-heading">Restart the server</h2>
            <p className="subtle">
              Picks up a new listen address, and settings changed from the command line (<code>agent-server config import</code>).
              It waits for requests in flight; running agents and their terminals reconnect by themselves.
            </p>
          </div>
          <div className="row wrap">
            <RestartButton status={status.data} />
            <span className="subtle">Running since {new Date(status.data.started_at * 1000).toLocaleString()}</span>
          </div>
        </section>
      )}
    </div>
  );
}
