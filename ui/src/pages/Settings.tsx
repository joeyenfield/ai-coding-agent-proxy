import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type NetworkConfig } from "../api";

interface Row {
  id: number;
  name: string;
  url: string;
  metricsUrl: string;
}

let nextId = 0;

export function SettingsPage() {
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const client = useQueryClient();
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [url, setUrl] = useState("");
  const [defaultBackend, setDefaultBackend] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [message, setMessage] = useState<{ text: string; tone: "good" | "warn" | "bad" } | null>(null);

  useEffect(() => {
    if (!config.data) return;
    const value = config.data.config;
    setHost(value.proxy.listen_host);
    setPort(String(value.proxy.listen_port));
    setUrl(value.proxy.url);
    setDefaultBackend(value.default_backend);
    setRows(Object.entries(value.backends).map(([name, backend]) => ({ id: nextId++, name, url: backend.url, metricsUrl: backend.metrics_url ?? "" })));
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
      backends[row.name.trim()] = { type: "ollama", url: row.url.trim(), ...(row.metricsUrl.trim() ? { metrics_url: row.metricsUrl.trim() } : {}) };
    }
    setMessage(null);
    save.mutate({
      proxy: { listen_host: host.trim(), listen_port: Number(port), url: url.trim() },
      default_backend: names.includes(defaultBackend) ? defaultBackend : names[0] ?? "",
      backends,
    });
  };
  const update = (id: number, patch: Partial<Row>) => setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));

  return (
    <div className="page">
      <header className="page-head">
        <h1>Settings</h1>
        <p className="lede">
          Network settings are saved to <code>config/backends.yaml</code>. Backend changes apply right away.
        </p>
      </header>
      {config.isError && <p className="bad">{config.error.message}</p>}
      <form onSubmit={submit}>
        <section className="section">
          <h2>Proxy address</h2>
          <div className="form-grid">
            <label className="field">
              Listen host
              <input value={host} onChange={(event) => setHost(event.target.value)} required />
              <small>Use 0.0.0.0 to accept connections from other machines.</small>
            </label>
            <label className="field">
              Listen port
              <input type="number" min={1} max={65535} value={port} onChange={(event) => setPort(event.target.value)} required />
            </label>
            <label className="field span-2">
              Address agents use
              <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} required />
              <small>The URL the launcher gives agents. Use this machine's LAN address when sharing the proxy.</small>
            </label>
          </div>
        </section>
        <section className="section">
          <h2>Ollama backends</h2>
          <p className="subtle">
            The metrics URL is only needed to chart CPU and GPU load on another machine; run <code>agent-metrics</code> there.
          </p>
          <div className="backend-rows">
            {rows.map((row) => (
              <div key={row.id} className="backend-row">
                <label className="field">
                  Name
                  <input value={row.name} onChange={(event) => update(row.id, { name: event.target.value })} required pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" />
                </label>
                <label className="field">
                  URL
                  <input type="url" value={row.url} placeholder="http://host:11434" onChange={(event) => update(row.id, { url: event.target.value })} required />
                </label>
                <label className="field">
                  Metrics URL
                  <input type="url" value={row.metricsUrl} placeholder="Optional, http://host:8182" onChange={(event) => update(row.id, { metricsUrl: event.target.value })} />
                </label>
                <label className="check">
                  <input type="radio" name="default" checked={defaultBackend === row.name} onChange={() => setDefaultBackend(row.name)} />
                  Default
                </label>
                <button type="button" disabled={rows.length === 1} onClick={() => setRows((current) => current.filter((item) => item.id !== row.id))}>
                  Remove
                </button>
              </div>
            ))}
          </div>
          <button type="button" onClick={() => setRows((current) => [...current, { id: nextId++, name: "", url: "", metricsUrl: "" }])}>
            Add backend
          </button>
        </section>
        <div className="row sticky-actions">
          <button className="primary" type="submit" disabled={save.isPending || !config.data}>
            Save settings
          </button>
          {message && (
            <span className={message.tone} role="status">
              {message.text}
            </span>
          )}
        </div>
      </form>
    </div>
  );
}
