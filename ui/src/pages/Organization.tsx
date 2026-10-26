import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Identity, type Org } from "../api";
import { CodeLine } from "../components/CodeLine";
import { PartTag } from "../components/PartTag";
import { formatDate } from "../format";
import { useAuth, useClipboard, useStatus, useToast } from "../hooks";

export function OrganizationPage() {
  const auth = useAuth();
  const identity = auth.data?.identity;
  const org = useQuery({ queryKey: ["org"], queryFn: api.org, enabled: Boolean(identity) });
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>{org.data?.name ?? "Organization"}</h1>
        <p className="lede">
          The people who use this proxy, in teams. Whoever starts a session chooses who sees it: only them, one of their
          teams, or everyone here.
        </p>
      </header>
      {auth.data && !auth.data.required && <AccountsOff />}
      {identity && (
        <>
          <YourAccount identity={identity} />
          {org.data && <Teams org={org.data} admin={identity.role === "admin"} />}
          {org.data && <Users org={org.data} identity={identity} />}
          {org.isError && <p className="bad">{org.error.message}</p>}
        </>
      )}
    </div>
  );
}

function AccountsOff() {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Accounts are off</h2>
        <p className="subtle">
          This proxy is set up for one person on one computer: anyone who can reach it can use it. Add a user on the
          machine running it to share it with others. Everyone then signs in, the first user is an admin, and each session
          is private until its owner shares it.
        </p>
      </div>
      <CodeLine text="agent-server user add <name> --team <team>" onCopy={() => navigator.clipboard?.writeText("agent-server user add ")} />
      <p className="subtle">
        On other computers, sign the launcher in once with <code>agent login http://&lt;server&gt;:{window.location.port || "80"}</code>.
      </p>
    </section>
  );
}

function YourAccount({ identity }: { identity: Identity }) {
  const notify = useToast();
  const copy = useClipboard();
  const client = useQueryClient();
  const status = useStatus();
  const tokens = useQuery({ queryKey: ["tokens"], queryFn: api.tokens });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [label, setLabel] = useState("");
  const [issued, setIssued] = useState<string | null>(null);
  const password = useMutation({
    mutationFn: () => api.changePassword(current, next),
    onSuccess: () => {
      notify("Password changed. Other browsers are signed out.");
      setCurrent("");
      setNext("");
    },
    onError: (error) => notify(error.message),
  });
  const create = useMutation({
    mutationFn: () => api.createToken(label.trim() || "launcher"),
    onSuccess: (result) => {
      setIssued(result.token);
      setLabel("");
      client.invalidateQueries({ queryKey: ["tokens"] });
    },
  });
  const revoke = useMutation({
    mutationFn: api.revokeToken,
    onSuccess: () => client.invalidateQueries({ queryKey: ["tokens"] }),
  });
  // The address this browser reached the server on works from other machines; the configured URL may be loopback.
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const server = loopback ? status.data?.proxy_url ?? window.location.origin : window.location.origin;
  return (
    <section className="panel" aria-labelledby="you-heading">
      <div className="panel-head">
        <h2 id="you-heading">You: {identity.user}</h2>
        <p className="subtle">
          {identity.role === "admin" ? "Admin: you manage this server, its users and teams." : "Member."}{" "}
          {identity.teams.length ? `Teams: ${identity.teams.join(", ")}.` : "Not in a team yet."}
        </p>
      </div>
      <h3>Start agents from your own computer</h3>
      <p className="subtle">Sign the launcher in once; it then starts every agent through this server and shares its terminal here.</p>
      <CodeLine text={`agent login ${server}`} onCopy={() => copy(`agent login ${server}`, "Command copied")} />
      {tokens.data && tokens.data.tokens.length > 0 && (
        <table className="table compact">
          <thead>
            <tr>
              <th>Launcher token</th>
              <th>Created</th>
              <th>Last used</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tokens.data.tokens.map((token) => (
              <tr key={token.id}>
                <td>{token.label}</td>
                <td>{formatDate(new Date(token.created_at * 1000).toISOString())}</td>
                <td>{token.last_used_at ? formatDate(new Date(token.last_used_at * 1000).toISOString()) : "Never"}</td>
                <td>
                  <button type="button" className="danger" onClick={() => revoke.mutate(token.id)}>
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <details>
        <summary>Make a token by hand, for scripts</summary>
        <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), create.mutate())}>
          <input value={label} onChange={(event) => setLabel(event.target.value)} placeholder="What it's for" aria-label="Token label" />
          <button type="submit" disabled={create.isPending}>
            Create token
          </button>
        </form>
        {issued && (
          <>
            <p className="warn">Copy it now; it isn't shown again. Send it as the X-AI-Proxy-Token header, or set AI_PROXY_TOKEN for the launcher.</p>
            <CodeLine text={issued} onCopy={() => copy(issued, "Token copied")} />
          </>
        )}
      </details>
      <h3>Change your password</h3>
      <form className="form-grid two" onSubmit={(event: FormEvent) => (event.preventDefault(), password.mutate())}>
        <label className="field">
          Current password
          <input type="password" value={current} onChange={(event) => setCurrent(event.target.value)} autoComplete="current-password" required />
        </label>
        <label className="field">
          New password (8 characters or more)
          <input type="password" value={next} onChange={(event) => setNext(event.target.value)} autoComplete="new-password" minLength={8} required />
        </label>
        <button type="submit" disabled={password.isPending}>
          Change password
        </button>
      </form>
    </section>
  );
}

function useOrgChange<T>(change: (value: T) => Promise<Org>) {
  const client = useQueryClient();
  const notify = useToast();
  return useMutation({
    mutationFn: change,
    onSuccess: (org) => {
      client.setQueryData(["org"], org);
      client.invalidateQueries({ queryKey: ["auth"] });
    },
    onError: (error) => notify(error.message),
  });
}

function Teams({ org, admin }: { org: Org; admin: boolean }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const add = useOrgChange(() => api.addTeam(id.trim(), name.trim() || id.trim()));
  const remove = useOrgChange(api.removeTeam);
  const rename = useOrgChange(api.renameOrg);
  const [orgName, setOrgName] = useState(org.name);
  return (
    <section className="section" aria-labelledby="teams-heading">
      <h2 id="teams-heading">Teams</h2>
      {org.teams.length ? (
        <ul className="team-list">
          {org.teams.map((team) => (
            <li key={team.id} className="team-card">
              <div className="row wrap">
                <strong>{team.name}</strong>
                <span className="mono subtle">{team.id}</span>
              </div>
              <p className="subtle">{team.members.length ? team.members.join(", ") : "No members yet"}</p>
              {admin && (
                <button type="button" className="danger" onClick={() => remove.mutate(team.id)}>
                  Remove team
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="subtle">No teams yet.</p>
      )}
      {admin && (
        <>
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), add.mutate(undefined), setId(""), setName(""))}>
            <input value={id} onChange={(event) => setId(event.target.value)} placeholder="Team id, such as core" aria-label="Team id" pattern="[a-z0-9][a-z0-9._\-]*" required />
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Name (optional)" aria-label="Team name" />
            <button type="submit">Add team</button>
          </form>
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(orgName))}>
            <input value={orgName} onChange={(event) => setOrgName(event.target.value)} aria-label="Organization name" />
            <button type="submit">Rename the organization</button>
          </form>
        </>
      )}
    </section>
  );
}

function Users({ org, identity }: { org: Org; identity: Identity }) {
  const admin = identity.role === "admin";
  const update = useOrgChange(({ name, ...body }: { name: string; role?: string; teams?: string[]; password?: string }) => api.updateUser(name, body));
  const remove = useOrgChange(api.removeUser);
  const add = useOrgChange(api.addUser);
  const [draft, setDraft] = useState({ name: "", password: "", role: "member", teams: [] as string[] });
  const toggle = (list: string[], team: string) => (list.includes(team) ? list.filter((item) => item !== team) : [...list, team]);
  return (
    <section className="section" aria-labelledby="users-heading">
      <h2 id="users-heading">Users</h2>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              <th>Teams</th>
              {admin && <th />}
            </tr>
          </thead>
          <tbody>
            {org.users.map((user) => (
              <tr key={user.name}>
                <td>
                  <strong>{user.name}</strong>
                  {user.name === identity.user && <span className="subtle"> (you)</span>}
                </td>
                <td>
                  {admin ? (
                    <select value={user.role} aria-label={`${user.name}'s role`} onChange={(event) => update.mutate({ name: user.name, role: event.target.value })}>
                      <option value="member">Member</option>
                      <option value="admin">Admin</option>
                    </select>
                  ) : user.role === "admin" ? "Admin" : "Member"}
                </td>
                <td>
                  {admin ? (
                    <span className="row wrap">
                      {org.teams.map((team) => (
                        <label key={team.id} className="check">
                          <input type="checkbox" checked={user.teams.includes(team.id)} onChange={() => update.mutate({ name: user.name, teams: toggle(user.teams, team.id) })} />
                          {team.id}
                        </label>
                      ))}
                    </span>
                  ) : (
                    user.teams.join(", ") || "–"
                  )}
                </td>
                {admin && (
                  <td className="row wrap">
                    <button
                      type="button"
                      onClick={() => {
                        const password = window.prompt(`New password for ${user.name} (8 characters or more)`);
                        if (password) update.mutate({ name: user.name, password });
                      }}
                    >
                      Set password
                    </button>
                    {user.name !== identity.user && (
                      <button type="button" className="danger" onClick={() => window.confirm(`Remove ${user.name}?`) && remove.mutate(user.name)}>
                        Remove
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {admin && (
        <form
          className="panel"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            add.mutate(draft, { onSuccess: () => setDraft({ name: "", password: "", role: "member", teams: [] }) });
          }}
        >
          <h3>Add a user</h3>
          <div className="form-grid two">
            <label className="field">
              Username
              <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} pattern="[a-z0-9][a-z0-9._\-]*" autoCapitalize="none" required />
            </label>
            <label className="field">
              Password (they can change it)
              <input type="password" value={draft.password} onChange={(event) => setDraft({ ...draft, password: event.target.value })} minLength={8} autoComplete="new-password" required />
            </label>
            <label className="field">
              Role
              <select value={draft.role} onChange={(event) => setDraft({ ...draft, role: event.target.value })}>
                <option value="member">Member: uses the proxy</option>
                <option value="admin">Admin: also manages it</option>
              </select>
            </label>
            <fieldset className="field">
              <legend>Teams</legend>
              <span className="row wrap">
                {org.teams.map((team) => (
                  <label key={team.id} className="check">
                    <input type="checkbox" checked={draft.teams.includes(team.id)} onChange={() => setDraft({ ...draft, teams: toggle(draft.teams, team.id) })} />
                    {team.id}
                  </label>
                ))}
              </span>
            </fieldset>
          </div>
          <button type="submit" className="primary" disabled={add.isPending}>
            Add user
          </button>
        </form>
      )}
    </section>
  );
}
