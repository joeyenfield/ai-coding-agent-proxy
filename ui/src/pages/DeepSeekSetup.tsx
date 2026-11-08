import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type ApiProvider, type ApiProviderFields, type ApiProviderSave, type ApiProvidersResponse, type Org } from "../api";
import { useToast } from "../hooks";

/** DeepSeek's connection flow uses the server preset; credentials go only to the server. */
export function DeepSeekSetup({ provider, initialName, initial, data, org, onDone }: {
  provider?: ApiProvider;
  initialName: string;
  initial: ApiProviderFields;
  data: ApiProvidersResponse;
  org: Org;
  onDone: (saved: string | null) => void;
}) {
  const client = useQueryClient();
  const notify = useToast();
  const [name, setName] = useState(initialName);
  const [key, setKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [source, setSource] = useState<"stored" | "env">((initial.key_env && provider?.key_source !== "vault") || !data.vault ? "env" : "stored");
  const [keyEnv, setKeyEnv] = useState(initial.key_env || "AI_PROXY_API_KEY_DEEPSEEK");
  const [teams, setTeams] = useState(initial.teams);
  const save = useMutation({
    mutationFn: async (body: ApiProviderSave) => {
      const saved = await api.saveApiProvider(org.id, name.trim(), body);
      setKey("");
      // A failed discovery must not pretend saving failed: the connection already exists.
      try {
        const result = await api.testApiProvider(org.id, saved.name);
        return { name: saved.name, message: result.ok
          ? `DeepSeek connected. ${result.models?.length ?? 0} models available.`
          : `DeepSeek saved, but the connection check failed: ${result.error ?? "try Refresh models on its card."}` };
      } catch (error) {
        return { name: saved.name, message: `DeepSeek saved. Couldn't check the connection: ${error instanceof Error ? error.message : "try Refresh models on its card."}` };
      }
    },
    onSuccess: (result) => {
      void client.invalidateQueries();
      notify(result.message);
      onDone(result.name);
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const body: ApiProviderSave = {
      ...initial,
      key_env: source === "env" ? keyEnv.trim() : null,
      teams: teams.filter((team) => org.teams.some((item) => item.key === team)),
    };
    if (source === "stored" && key.trim()) body.key = key.trim();
    // A stored key wins over an environment variable on the server; explicitly switch sources.
    if (source === "env" && provider?.key_source === "vault") body.clear_key = true;
    save.mutate(body);
  };
  return <section className="deepseek-setup" aria-label="DeepSeek setup">
    <header className="deepseek-heading">
      <span className="deepseek-mark" aria-hidden="true">D</span>
      <div><h3>{provider ? "Your DeepSeek connection" : "Connect DeepSeek"}</h3>
        <p>Use your DeepSeek account with Claude Code and compatible coding agents.</p></div>
    </header>
    <div className="deepseek-features">
      <div><strong>Models, automatically</strong><p>Available models are fetched from your DeepSeek account.</p></div>
      <div><strong>Ready for Claude Code</strong><p>The official connection and model context settings are handled for you.</p></div>
      <div><strong>Check your balance</strong><p>See available funds and balances by currency after connecting.</p></div>
    </div>
    <form className="deepseek-form" onSubmit={submit}>
      <fieldset className="field">
        <legend>DeepSeek API key</legend>
        <p className="subtle">Create a key in your <a href="https://platform.deepseek.com" target="_blank" rel="noreferrer">DeepSeek account</a>. Requests use that account's balance.</p>
        <div className="row wrap">
          <label className="check"><input type="radio" name={`deepseek-key-${initialName}`} checked={source === "stored"} disabled={!data.vault || save.isPending} onChange={() => setSource("stored")} />Save securely on this server</label>
          <label className="check"><input type="radio" name={`deepseek-key-${initialName}`} checked={source === "env"} disabled={save.isPending} onChange={() => setSource("env")} />Use a server environment variable</label>
        </div>
        {source === "stored" ? <>
          <label className="field">{provider?.key_source === "vault" ? "Replace API key" : "API key"}
            <div className="deepseek-key"><input aria-label="DeepSeek API key" type={showKey ? "text" : "password"} value={key} onChange={(event) => setKey(event.target.value)}
              placeholder={provider?.key_source === "vault" ? "Leave blank to keep the saved key" : "sk-…"} autoComplete="new-password" spellCheck={false}
              required={provider?.key_source !== "vault"} disabled={save.isPending} />
              <button type="button" aria-pressed={showKey} onClick={() => setShowKey(!showKey)}>{showKey ? "Hide" : "Show"}</button></div>
          </label>
          <small>The key is encrypted on the server. It is never sent to agents or shown again.</small>
        </> : <label className="field">Environment variable name
          <input value={keyEnv} onChange={(event) => setKeyEnv(event.target.value)} required pattern="AI_PROXY_API_KEY_[A-Z0-9_]+" autoCapitalize="none" spellCheck={false} disabled={save.isPending} />
          <small>Set this variable to your DeepSeek API key on the server, then restart the server. Enter the variable's name here.</small>
          {provider?.key_source === "vault" && <small>Saving with this source removes the previously saved key.</small>}
        </label>}
        {!data.vault && <p className="callout">Secure key storage isn't configured on this server. Use an environment variable, or follow the <a href="/help/subscriptions#the-vault">vault setup guide</a> to enable storage.</p>}
      </fieldset>
      <details className="deepseek-access">
        <summary>Connection name and access</summary>
        <div className="stack">
          <label className="field">Connection name
            <input value={name} onChange={(event) => setName(event.target.value)} pattern="[A-Za-z0-9_-]+" required disabled={Boolean(provider) || save.isPending} autoCapitalize="none" />
            <small>Use this name when choosing DeepSeek for an agent.</small>
          </label>
          {org.teams.length > 0 && <fieldset className="field"><legend>Teams that may use this connection</legend>
            <div className="row wrap">{org.teams.map((team) => <label className="check" key={team.key}>
              <input type="checkbox" checked={teams.includes(team.key)} disabled={save.isPending} onChange={() => setTeams((current) => current.includes(team.key) ? current.filter((key) => key !== team.key) : [...current, team.key])} />{team.name}
            </label>)}</div><small>Leave all teams unchecked to allow everyone in {org.name}. Its administrators always have access.</small>
          </fieldset>}
        </div>
      </details>
      <p className="subtle">{provider ? "Saving checks your connection and refreshes the available models." : "Connecting saves your key, checks DeepSeek, and fetches the available models. You can choose a model when starting an agent."}</p>
      <div className="row wrap">
        <button className="primary" disabled={save.isPending} type="submit">{save.isPending ? "Connecting to DeepSeek…" : provider ? "Save and check connection" : "Connect DeepSeek"}</button>
        <button type="button" disabled={save.isPending} onClick={() => onDone(null)}>Cancel</button>
      </div>
      {save.isError && <p className="bad" role="alert">{save.error.message}</p>}
    </form>
  </section>;
}
