import { useMemo, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Directory, type Identity, type Org, type User } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PartTag } from "../components/PartTag";
import { formatDate } from "../format";
import { useAuth, useClipboard, useStatus, useToast } from "../hooks";

const NAME_PATTERN = "[a-z0-9][a-z0-9._\\-]*";

export function OrganizationPage() {
  const auth = useAuth();
  const identity = auth.data?.identity;
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs, enabled: Boolean(identity) });
  const users = useQuery({ queryKey: ["users"], queryFn: api.users, enabled: Boolean(identity) });
  const error = orgs.error ?? users.error;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Organizations and users</h1>
        <p className="lede">
          Users are the people who sign in. Each can belong to several organizations, and to teams inside them. Whoever
          starts a session chooses who sees it: only them, one of their teams, or everyone in one of their organizations.
        </p>
      </header>
      {auth.data && !auth.data.required && <AccountsOff />}
      {identity && (
        <>
          <YourAccount identity={identity} />
          {orgs.data && users.data && (
            <>
              <Organizations orgs={orgs.data.orgs} users={users.data.users} identity={identity} />
              <Users orgs={orgs.data.orgs} users={users.data.users} identity={identity} />
            </>
          )}
          {error && <p className="bad">{error.message}</p>}
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
      <CodeLine text="agent-server user add <name> --org <organization>" onCopy={() => navigator.clipboard?.writeText("agent-server user add ")} />
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
          {identity.role === "admin" ? "Admin: you manage this server, its organizations and users." : "Member."}{" "}
          {identity.orgs.length ? `Organizations: ${identity.orgs.join(", ")}.` : "Not in an organization yet."}{" "}
          {identity.teams.length > 0 && `Teams: ${identity.teams.join(", ")}.`}
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

/** A change to organizations or users; the server answers with everything, which replaces both lists. */
function useDirectoryChange<T>(change: (value: T) => Promise<Directory>) {
  const client = useQueryClient();
  const notify = useToast();
  return useMutation({
    mutationFn: change,
    onSuccess: (directory) => {
      client.setQueryData(["orgs"], { orgs: directory.orgs });
      client.setQueryData(["users"], { users: directory.users });
      client.invalidateQueries({ queryKey: ["auth"] });
    },
    onError: (error) => notify(error.message),
  });
}

function matches(query: string, ...values: string[]): boolean {
  const wanted = query.trim().toLowerCase();
  return !wanted || values.some((value) => value.toLowerCase().includes(wanted));
}

function Organizations({ orgs, users, identity }: { orgs: Org[]; users: User[]; identity: Identity }) {
  const admin = identity.role === "admin";
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState({ id: "", name: "" });
  const [deleting, setDeleting] = useState<Org | null>(null);
  const add = useDirectoryChange(() => api.addOrg(draft.id.trim(), draft.name.trim() || draft.id.trim()));
  const remove = useDirectoryChange(api.removeOrg);
  const shown = orgs.filter((org) => matches(query, org.id, org.name, ...org.members));
  const selected = orgs.find((org) => org.id === params.get("org")) ?? null;
  const select = (id: string | null) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (id) next.set("org", id);
        else next.delete("org");
        return next;
      },
      { replace: true },
    );
  return (
    <section className="section" aria-labelledby="orgs-heading">
      <div className="section-head">
        <h2 id="orgs-heading">Organizations</h2>
        <span className="subtle">{admin ? `${orgs.length} on this server` : "The ones you belong to"}</span>
      </div>
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search organizations or their members" aria-label="Search organizations" />
      </div>
      {admin && (
        <form
          className="inline-form"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            add.mutate(undefined, {
              onSuccess: () => {
                select(draft.id.trim().toLowerCase());
                setDraft({ id: "", name: "" });
              },
            });
          }}
        >
          <input value={draft.id} onChange={(event) => setDraft({ ...draft, id: event.target.value })} placeholder="Organization id, such as acme" aria-label="Organization id" pattern={NAME_PATTERN} autoCapitalize="none" required />
          <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="Name (optional)" aria-label="Organization name" />
          <button type="submit" className="primary" disabled={add.isPending}>
            Create organization
          </button>
        </form>
      )}
      {shown.length ? (
        <ul className="team-list">
          {shown.map((org) => (
            <li key={org.id} className={`team-card org-card${selected?.id === org.id ? " selected" : ""}`}>
              <button type="button" className="link org-open" aria-expanded={selected?.id === org.id} onClick={() => select(selected?.id === org.id ? null : org.id)}>
                <strong>{org.name}</strong>
              </button>
              <span className="mono subtle">{org.id}</span>
              <span className="subtle">
                {plural(org.members.length, "member")} · {plural(org.teams.length, "team")}
              </span>
              {admin && (
                <button type="button" className="danger" onClick={() => setDeleting(org)}>
                  Delete
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="subtle">
          {orgs.length ? "No organizations match." : admin ? "No organizations yet. Create one below." : "You aren't in an organization yet; ask an admin to add you to one."}
        </p>
      )}
      {selected && <OrgDetail key={selected.id} org={selected} users={users} admin={admin} onClose={() => select(null)} />}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name ?? ""}?`}
        confirmLabel="Delete organization"
        busy={remove.isPending}
        onCancel={() => setDeleting(null)}
        onConfirm={() =>
          deleting &&
          remove.mutate(deleting.id, {
            onSuccess: () => {
              if (selected?.id === deleting.id) select(null);
              setDeleting(null);
            },
          })
        }
      >
        <p>
          Its {plural(deleting?.teams.length ?? 0, "team")} go too, and its {plural(deleting?.members.length ?? 0, "member")} leave it. Their accounts
          stay. Sessions shared with it are then seen only by their owners.
        </p>
      </ConfirmDialog>
    </section>
  );
}

function OrgDetail({ org, users, admin, onClose }: { org: Org; users: User[]; admin: boolean; onClose: () => void }) {
  const [name, setName] = useState(org.name);
  const [team, setTeam] = useState({ id: "", name: "" });
  const [adding, setAdding] = useState("");
  const rename = useDirectoryChange(() => api.renameOrg(org.id, name));
  const addTeam = useDirectoryChange(() => api.addTeam(org.id, team.id.trim(), team.name.trim() || team.id.trim()));
  const removeTeam = useDirectoryChange((id: string) => api.removeTeam(org.id, id));
  const membership = useDirectoryChange(({ user, orgs }: { user: User; orgs: string[] }) => api.updateUser(user.name, { orgs }));
  const byName = useMemo(() => new Map(users.map((user) => [user.name, user])), [users]);
  const outside = users.filter((user) => !user.orgs.includes(org.id));
  return (
    <div className="panel org-detail" aria-labelledby="org-detail-heading">
      <div className="section-head">
        <h3 id="org-detail-heading">
          {org.name} <span className="mono subtle">{org.id}</span>
        </h3>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
      {admin && (
        <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
          <input value={name} onChange={(event) => setName(event.target.value)} aria-label="Organization name" />
          <button type="submit" disabled={rename.isPending || name.trim() === org.name}>
            Rename
          </button>
        </form>
      )}
      <h4>Members</h4>
      {org.members.length ? (
        <ul className="member-list">
          {org.members.map((member) => {
            const user = byName.get(member);
            return (
              <li key={member}>
                <span>
                  <strong>{member}</strong>
                  {user?.role === "admin" && <span className="pill accent">Admin</span>}
                </span>
                {admin && user && (
                  <button type="button" className="danger" onClick={() => membership.mutate({ user, orgs: user.orgs.filter((item) => item !== org.id) })}>
                    Remove from {org.id}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="subtle">No members yet.</p>
      )}
      {admin && outside.length > 0 && (
        <form
          className="inline-form"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            const user = byName.get(adding);
            if (user) membership.mutate({ user, orgs: [...user.orgs, org.id] }, { onSuccess: () => setAdding("") });
          }}
        >
          <select value={adding} onChange={(event) => setAdding(event.target.value)} aria-label="User to add" required>
            <option value="">Choose a user…</option>
            {outside.map((user) => (
              <option key={user.name} value={user.name}>
                {user.name}
              </option>
            ))}
          </select>
          <button type="submit" disabled={!adding || membership.isPending}>
            Add member
          </button>
        </form>
      )}
      <h4>Teams</h4>
      {org.teams.length ? (
        <ul className="member-list">
          {org.teams.map((item) => (
            <li key={item.key}>
              <span>
                <strong>{item.name}</strong> <span className="mono subtle">{item.key}</span>
                <br />
                <span className="subtle">{item.members.length ? item.members.join(", ") : "No members yet"}</span>
              </span>
              {admin && (
                <button type="button" className="danger" onClick={() => removeTeam.mutate(item.id)}>
                  Remove team
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="subtle">No teams yet. Teams let someone share a session with part of an organization.</p>
      )}
      {admin && (
        <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), addTeam.mutate(undefined, { onSuccess: () => setTeam({ id: "", name: "" }) }))}>
          <input value={team.id} onChange={(event) => setTeam({ ...team, id: event.target.value })} placeholder="Team id, such as core" aria-label="Team id" pattern={NAME_PATTERN} autoCapitalize="none" required />
          <input value={team.name} onChange={(event) => setTeam({ ...team, name: event.target.value })} placeholder="Name (optional)" aria-label="Team name" />
          <button type="submit" disabled={addTeam.isPending}>
            Add team
          </button>
        </form>
      )}
    </div>
  );
}

/** Which organizations, and teams inside them, a user is in. A team shows once its organization is ticked. */
function Membership({ orgs, value, onChange }: { orgs: Org[]; value: { orgs: string[]; teams: string[] }; onChange: (value: { orgs: string[]; teams: string[] }) => void }) {
  const [query, setQuery] = useState("");
  const toggle = (list: string[], item: string) => (list.includes(item) ? list.filter((other) => other !== item) : [...list, item]);
  const shown = orgs.filter((org) => value.orgs.includes(org.id) || matches(query, org.id, org.name));
  if (!orgs.length) return <p className="subtle">No organizations yet. Create one above.</p>;
  return (
    <div className="membership">
      {orgs.length > 6 && <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find an organization" aria-label="Find an organization" />}
      {shown.map((org) => {
        const member = value.orgs.includes(org.id);
        return (
          <div key={org.id} className="membership-org">
            <label className="check">
              <input
                type="checkbox"
                checked={member}
                onChange={() =>
                  onChange({
                    orgs: toggle(value.orgs, org.id),
                    teams: member ? value.teams.filter((team) => !team.startsWith(`${org.id}/`)) : value.teams,
                  })
                }
              />
              {org.name}
            </label>
            {member && org.teams.length > 0 && (
              <span className="row wrap membership-teams">
                {org.teams.map((team) => (
                  <label key={team.key} className="check">
                    <input type="checkbox" checked={value.teams.includes(team.key)} onChange={() => onChange({ ...value, teams: toggle(value.teams, team.key) })} />
                    {team.name}
                  </label>
                ))}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Users({ orgs, users, identity }: { orgs: Org[]; users: User[]; identity: Identity }) {
  const admin = identity.role === "admin";
  const [query, setQuery] = useState("");
  const [org, setOrg] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [removing, setRemoving] = useState<User | null>(null);
  const update = useDirectoryChange(({ name, ...body }: { name: string; role?: string; orgs?: string[]; teams?: string[]; password?: string }) => api.updateUser(name, body));
  const remove = useDirectoryChange(api.removeUser);
  const add = useDirectoryChange(api.addUser);
  const blank = { name: "", password: "", role: "member", orgs: [] as string[], teams: [] as string[] };
  const [draft, setDraft] = useState(blank);
  const names = useMemo(() => new Map(orgs.map((item) => [item.id, item.name])), [orgs]);
  const shown = users.filter((user) => (!org || user.orgs.includes(org)) && matches(query, user.name, ...user.orgs, ...user.teams));
  return (
    <section className="section" aria-labelledby="users-heading">
      <div className="section-head">
        <h2 id="users-heading">Users</h2>
        <span className="subtle">{admin ? `${users.length} can sign in` : "You and the people in your organizations"}</span>
      </div>
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search users" aria-label="Search users" />
        <select value={org} onChange={(event) => setOrg(event.target.value)} aria-label="Organization">
          <option value="">Every organization</option>
          {orgs.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      <div className="table-scroll">
        <table className="table users-table">
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              <th>Organizations and teams</th>
              {admin && <th />}
            </tr>
          </thead>
          <tbody>
            {shown.map((user) => (
              <UserRow
                key={user.name}
                user={user}
                orgs={orgs}
                names={names}
                identity={identity}
                editing={editing === user.name}
                onEdit={() => setEditing(editing === user.name ? null : user.name)}
                onChange={(body) => update.mutate({ name: user.name, ...body }, { onSuccess: () => body.orgs && setEditing(null) })}
                onRemove={() => setRemoving(user)}
              />
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={admin ? 4 : 3} className="subtle">
                  No users match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {admin && (
        <form
          className="panel"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            add.mutate(draft, { onSuccess: () => setDraft(blank) });
          }}
        >
          <h3>Add a user</h3>
          <div className="form-grid two">
            <label className="field">
              Username
              <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} pattern={NAME_PATTERN} autoCapitalize="none" required />
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
          </div>
          <fieldset className="field">
            <legend>Organizations and teams</legend>
            <Membership orgs={orgs} value={draft} onChange={(value) => setDraft({ ...draft, ...value })} />
          </fieldset>
          <button type="submit" className="primary" disabled={add.isPending}>
            Add user
          </button>
        </form>
      )}
      <ConfirmDialog
        open={removing !== null}
        title={`Remove ${removing?.name ?? ""}?`}
        confirmLabel="Remove user"
        busy={remove.isPending}
        onCancel={() => setRemoving(null)}
        onConfirm={() => removing && remove.mutate(removing.name, { onSuccess: () => setRemoving(null) })}
      >
        <p>They're signed out everywhere and their launcher tokens stop working. Their sessions stay.</p>
      </ConfirmDialog>
    </section>
  );
}

function UserRow({
  user,
  orgs,
  names,
  identity,
  editing,
  onEdit,
  onChange,
  onRemove,
}: {
  user: User;
  orgs: Org[];
  names: Map<string, string>;
  identity: Identity;
  editing: boolean;
  onEdit: () => void;
  onChange: (body: { role?: string; orgs?: string[]; teams?: string[]; password?: string }) => void;
  onRemove: () => void;
}) {
  const admin = identity.role === "admin";
  const [membership, setMembership] = useState({ orgs: user.orgs, teams: user.teams });
  return (
    <>
      <tr className={editing ? "selected" : undefined}>
        <td>
          <strong>{user.name}</strong>
          {user.name === identity.user && <span className="subtle"> (you)</span>}
        </td>
        <td>
          {admin ? (
            <select value={user.role} aria-label={`${user.name}'s role`} onChange={(event) => onChange({ role: event.target.value })}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          ) : user.role === "admin" ? (
            "Admin"
          ) : (
            "Member"
          )}
        </td>
        <td>
          {user.orgs.length ? (
            <span className="row wrap">
              {user.orgs.map((org) => (
                <span key={org} className="pill accent" title={org}>
                  {names.get(org) ?? org}
                </span>
              ))}
              {user.teams.map((team) => (
                <span key={team} className="pill">
                  {team}
                </span>
              ))}
            </span>
          ) : (
            <span className="subtle">None</span>
          )}
        </td>
        {admin && (
          <td>
            <span className="row wrap">
              <button
                type="button"
                aria-expanded={editing}
                onClick={() => {
                  setMembership({ orgs: user.orgs, teams: user.teams });
                  onEdit();
                }}
              >
                {editing ? "Cancel" : "Organizations"}
              </button>
              <button
                type="button"
                onClick={() => {
                  const password = window.prompt(`New password for ${user.name} (8 characters or more)`);
                  if (password) onChange({ password });
                }}
              >
                Set password
              </button>
              {user.name !== identity.user && (
                <button type="button" className="danger" onClick={onRemove}>
                  Remove
                </button>
              )}
            </span>
          </td>
        )}
      </tr>
      {editing && (
        <tr className="selected">
          <td colSpan={4}>
            <form className="membership-edit" onSubmit={(event: FormEvent) => (event.preventDefault(), onChange(membership))}>
              <Membership orgs={orgs} value={membership} onChange={setMembership} />
              <button type="submit" className="primary">
                Save {user.name}'s organizations
              </button>
            </form>
          </td>
        </tr>
      )}
    </>
  );
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}
