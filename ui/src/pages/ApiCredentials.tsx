import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type User } from "../api";
import { pollEvery } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";

export function BootstrapCredentialWarning() {
  const query = useQuery({ queryKey: ["api-credentials", "root"], queryFn: () => api.apiCredentials("root"), refetchInterval: pollEvery(15000) });
  if (query.isError) return <p className="banner warn" role="alert">Unable to check bootstrap API credentials: {String(query.error)}</p>;
  if (!query.data?.credentials.some((item) => item.bootstrap && !item.revoked)) return null;
  return <p className="banner warn" role="alert">Bootstrap API credentials are still enabled. Revoke them after setup. <Link to="/organization?org=root&orgTab=credentials">Manage API credentials</Link></p>;
}

export function ApiCredentials({ org, users }: { org: string; users: User[] }) {
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["api-credentials", org], queryFn: () => api.apiCredentials(org), refetchInterval: pollEvery(15000) });
  const [user, setUser] = useState(users[0]?.name ?? "");
  const [label, setLabel] = useState("");
  const [bootstrap, setBootstrap] = useState(false);
  const [secret, setSecret] = useState<{ client_id: string; client_secret: string } | null>(null);
  const [pending, setPending] = useState<{ id: string; action: "revoke" | "regenerate" } | null>(null);
  const changed = () => cache.invalidateQueries({ queryKey: ["api-credentials"] });
  const create = useMutation({ mutationFn: () => api.createApiCredential(org, { user, label, bootstrap }), onSuccess: (value) => { setSecret(value); void changed(); } });
  const action = useMutation({ mutationFn: async () => {
    if (!pending) return;
    setSecret(null);
    if (pending.action === "regenerate") setSecret(await api.regenerateApiCredential(org, pending.id));
    else await api.revokeApiCredential(org, pending.id);
  }, onSuccess: () => { setPending(null); void changed(); } });
  return <section className="stack">
    <h3>API credentials</h3>
    <p>A client ID and secret let scripts set up test data and manage the API without a browser. Each credential acts as the selected account, with its organisation, role and team access. Choose an administrator to manage data. Site administrator credentials can configure the server and create organisations.</p>
    <p>Secrets are shown once. Password changes, account removal or role changes disable the credential. Regenerate replaces its secret and ends its existing tokens. Revoke permanently disables it; you can create more afterwards.</p>
    {query.isError && <p className="bad" role="alert">{String(query.error)}</p>}
    <form className="stack" onSubmit={(event) => { event.preventDefault(); setSecret(null); create.mutate(); }}>
      <label className="field">Description<input value={label} onChange={(event) => setLabel(event.target.value)} maxLength={80} required /></label>
      <label className="field">Act as account<select value={user} onChange={(event) => setUser(event.target.value)} required>
        {users.map((item) => <option key={item.name} value={item.name}>{item.name} ({org === "root" ? "site administrator" : item.role})</option>)}
      </select></label>
      {org === "root" && <label><input type="checkbox" checked={bootstrap} onChange={(event) => setBootstrap(event.target.checked)} /> Bootstrap credential: show a site administrator warning until revoked</label>}
      <div><button className="primary" disabled={!user || create.isPending}>Create credential</button></div>
    </form>
    {create.isError && <p className="bad" role="alert">{create.error.message}</p>}
    {secret && <div className="callout"><strong>Save this secret now. It will not be shown again.</strong><dl>
      <dt>Client ID</dt><dd><code>{secret.client_id}</code></dd><dt>Client secret</dt><dd><code style={{ overflowWrap: "anywhere" }}>{secret.client_secret}</code></dd>
    </dl><button onClick={() => setSecret(null)}>Dismiss secret</button></div>}
    <p>Exchange credentials using HTTP Basic at <code>{query.data?.token_endpoint ?? "the issuer's /token endpoint"}</code>, with form fields <code>grant_type=client_credentials</code> and <code>scope=mesh-api</code>. Send the returned access token to the API as <code>Authorization: Bearer &lt;access_token&gt;</code>. Request another token when it expires.</p>
    {query.data?.credentials.map((item) => <article className="panel" key={item.client_id}>
      <h3>{item.label || item.client_id}</h3><p><code>{item.client_id}</code></p>
      <p>{item.user} · {org === "root" ? "site administrator" : item.role} · {item.bootstrap ? "Bootstrap" : "API credential"} · {item.revoked ? "Revoked" : item.active ? "Enabled" : "Account changed: disabled"}</p>
      <div className="row wrap">
        <button disabled={!item.active || action.isPending} onClick={() => { action.reset(); setPending({ id: item.client_id, action: "regenerate" }); }}>Regenerate secret</button>
        <button className="danger" disabled={item.revoked || action.isPending} onClick={() => { action.reset(); setPending({ id: item.client_id, action: "revoke" }); }}>Revoke</button>
      </div>
    </article>)}
    {query.data && !query.data.credentials.length && <p className="subtle">No API credentials yet.</p>}
    <ConfirmDialog open={!!pending} title={pending?.action === "revoke" ? "Revoke API credential?" : "Regenerate client secret?"} confirmLabel={pending?.action === "revoke" ? "Revoke" : "Regenerate"} busy={action.isPending} error={action.error?.message} onCancel={() => setPending(null)} onConfirm={() => action.mutate()}>
      <p>Scripts using this credential and its existing access tokens will stop working immediately.</p>
    </ConfirmDialog>
  </section>;
}
