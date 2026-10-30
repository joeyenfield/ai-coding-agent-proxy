import { useMemo, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_ORIGIN, api, type Directory, type Identity, type Org, type Team, type User } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PartTag } from "../components/PartTag";
import { TabBar, TabPanel, useTabParam } from "../components/Tabs";
import { useAuth, useClipboard, useStatus, useToast } from "../hooks";

const NAME_PATTERN = "[a-z0-9][a-z0-9._\\-]*";

export function OrganizationPage() {
  const auth = useAuth();
  const identity = auth.data?.identity;
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs, enabled: Boolean(identity) });
  const users = useQuery({ queryKey: ["users"], queryFn: api.users, enabled: Boolean(identity) });
  const [tab, setTab] = useTabParam(["organizations", "users", "account"] as const);
  const error = orgs.error ?? users.error;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Organizations and users</h1>
        <p className="lede">
          Organizations bring people together. Teams group people within an organization for sharing sessions and machine access.
        </p>
      </header>
      {identity && (
        <>
          <TabBar
            label="Organization management"
            idPrefix="directory-"
            active={tab}
            onSelect={setTab}
            tabs={[{ id: "organizations", label: "Organizations" }, { id: "users", label: "User directory" }, { id: "account", label: "Your account" }]}
          />
          {(!orgs.data || !users.data) && !error && tab !== "account" && <p className="subtle">Loading directory…</p>}
          <TabPanel idPrefix="directory-" id="organizations" active={tab === "organizations"}>
            {orgs.data && users.data && (
              <Organizations orgs={orgs.data.orgs} users={users.data.users} identity={identity} />
            )}
          </TabPanel>
          <TabPanel idPrefix="directory-" id="users" active={tab === "users"}>
            {orgs.data && users.data && (
              <Users orgs={orgs.data.orgs} users={users.data.users} identity={identity} />
            )}
          </TabPanel>
          <TabPanel idPrefix="directory-" id="account" active={tab === "account"}>
            <YourAccount identity={identity} />
          </TabPanel>
          {error && <p className="bad">{error.message}</p>}
        </>
      )}
    </div>
  );
}

function YourAccount({ identity }: { identity: Identity }) {
  const copy = useClipboard();
  const status = useStatus();
  // The address this browser reached the server on works from other machines; the configured URL may be loopback.
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const server = API_ORIGIN || (loopback ? status.data?.proxy_url ?? window.location.origin : window.location.origin);
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
      <p className="subtle">Run this command, then approve the device code at the authentication service. The launcher renews its short-lived access tokens automatically.</p>
      <CodeLine text={`agent login ${server}`} onCopy={() => copy(`agent login ${server}`, "Command copied")} />
      <p className="subtle">For automation, an administrator provisions a service account at the issuer. API requests use a Bearer access token, never an ID token. Ask an administrator to reset your account password.</p>
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
      client.invalidateQueries({ queryKey: ["machines"] });
      client.invalidateQueries({ queryKey: ["sessions"] });
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
  const selected = shown.find((org) => org.id === params.get("org")) ?? shown[0] ?? null;
  const select = (id: string | null) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (id) next.set("org", id);
        else next.delete("org");
        next.delete("team");
        next.delete("orgTab");
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
        <details className="directory-create">
          <summary>Create organization</summary>
          <form
            className="inline-form"
            onSubmit={(event: FormEvent) => {
              event.preventDefault();
              add.mutate(undefined, {
                onSuccess: () => {
                  setQuery("");
                  select(draft.id.trim().toLowerCase());
                  setDraft({ id: "", name: "" });
                },
              });
            }}
          >
            <input value={draft.id} onChange={(event) => setDraft({ ...draft, id: event.target.value })} placeholder="Organization id, such as acme" aria-label="Organization id" pattern={NAME_PATTERN} maxLength={40} autoCapitalize="none" required />
            <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="Name (optional)" aria-label="Organization name" maxLength={80} />
            <button type="submit" className="primary" disabled={add.isPending}>
              Create organization
            </button>
          </form>
        </details>
      )}
      <div className="directory-workspace">
        <nav aria-label="Organizations">
          {shown.length ? (
            <ul className="org-list">
              {shown.map((org) => (
                <li key={org.id} className={`team-card org-card${selected?.id === org.id ? " selected" : ""}`}>
                  <button type="button" className="link org-open" aria-current={selected?.id === org.id ? "true" : undefined} onClick={() => select(org.id)}>
                    <strong>{org.name}</strong>
                  </button>
                  <span className="mono subtle">{org.id}</span>
                  <span className="subtle">
                    {plural(org.members.length, "member")} · {plural(org.teams.length, "team")}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="subtle">
              {orgs.length ? "No organizations match." : admin ? "Create an organization to start adding teams and people." : "You aren't in an organization yet; ask an admin to add you to one."}
            </p>
          )}
        </nav>
        {selected && <OrgDetail key={selected.id} org={selected} users={users} admin={admin} onDelete={() => setDeleting(selected)} />}
      </div>
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

function OrgDetail({ org, users, admin, onDelete }: { org: Org; users: User[]; admin: boolean; onDelete: () => void }) {
  const [name, setName] = useState(org.name);
  const [team, setTeam] = useState({ id: "", name: "" });
  const [tab, setTab] = useTabParam(["teams", "members", "settings"] as const, "orgTab");
  const [params, setParams] = useSearchParams();
  const selected = org.teams.find((item) => item.id === params.get("team"));
  const rename = useDirectoryChange(() => api.renameOrg(org.id, name));
  const addTeam = useDirectoryChange(() => api.addTeam(org.id, team.id.trim(), team.name.trim() || team.id.trim()));
  const selectTeam = (id: string | null) => setParams((current) => {
    const next = new URLSearchParams(current);
    if (id) next.set("team", id);
    else next.delete("team");
    // Pin the organization even when it was selected by default.
    next.set("org", org.id);
    return next;
  }, { replace: true });
  return (
    <div className="panel org-detail" aria-labelledby="org-detail-heading">
      <header>
        <p className="subtle">Organization · <span className="mono">{org.id}</span></p>
        <h2 id="org-detail-heading">{org.name}</h2>
        <p className="subtle">{plural(org.teams.length, "team")} · {plural(org.members.length, "member")}</p>
      </header>
      <TabBar label={`${org.name} sections`} idPrefix="org-" active={tab} onSelect={setTab}
        tabs={[{ id: "teams", label: "Teams" }, { id: "members", label: "Members" }, { id: "settings", label: "Settings" }]} />
      <TabPanel idPrefix="org-" id="teams" active={tab === "teams"}>
        {selected ? (
          <TeamDetail key={selected.key} org={org} team={selected} users={users} admin={admin} onBack={() => selectTeam(null)} />
        ) : (
          <>
            <p className="subtle">Teams belong to this organization. People can join more than one team.</p>
            {org.teams.length ? (
              <ul className="team-list">
                {org.teams.map((item) => (
                  <li key={item.key} className="team-card">
                    <button type="button" className="link org-open" onClick={() => selectTeam(item.id)}>{item.name}</button>
                    <span className="mono subtle">{item.key}</span>
                    <span className="subtle">{plural(item.members.length, "member")}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="subtle">No teams yet. Add a team to share sessions and machine access with a smaller group.</p>}
            {admin && (
              <form className="inline-form" onSubmit={(event: FormEvent) => {
                event.preventDefault();
                addTeam.mutate(undefined, { onSuccess: () => {
                  selectTeam(team.id.trim().toLowerCase());
                  setTeam({ id: "", name: "" });
                } });
              }}>
                <input value={team.id} onChange={(event) => setTeam({ ...team, id: event.target.value })} placeholder="Team id, such as core" aria-label="Team id" pattern={NAME_PATTERN} maxLength={40} autoCapitalize="none" required />
                <input value={team.name} onChange={(event) => setTeam({ ...team, name: event.target.value })} placeholder="Name (optional)" aria-label="Team name" maxLength={80} />
                <button type="submit" disabled={addTeam.isPending}>Add team</button>
              </form>
            )}
          </>
        )}
      </TabPanel>
      <TabPanel idPrefix="org-" id="members" active={tab === "members"}>
        <Members org={org} users={users} admin={admin} />
      </TabPanel>
      <TabPanel idPrefix="org-" id="settings" active={tab === "settings"}>
        <p className="subtle">The organization id stays the same when its display name changes. Server roles are managed in the user directory.</p>
        {admin ? (
          <>
            <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
              <label className="field">Organization name
                <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required />
              </label>
              <button type="submit" disabled={rename.isPending || name.trim() === org.name}>Save name</button>
            </form>
            <div className="directory-danger">
              <h3>Delete organization</h3>
              <p className="subtle">Removes its teams and memberships, not people's accounts or memberships elsewhere.</p>
              <button type="button" className="danger" onClick={onDelete}>Delete organization</button>
            </div>
          </>
        ) : <p className="subtle">Only a server admin can change this organization.</p>}
      </TabPanel>
    </div>
  );
}

function TeamDetail({ org, team, users, admin, onBack }: { org: Org; team: Team; users: User[]; admin: boolean; onBack: () => void }) {
  const [name, setName] = useState(team.name);
  const [deleting, setDeleting] = useState(false);
  const rename = useDirectoryChange(() => api.renameTeam(org.id, team.id, name));
  const remove = useDirectoryChange(() => api.removeTeam(org.id, team.id));
  return (
    <section className="team-detail" aria-label={`${team.name} team`}>
      <button type="button" className="link" onClick={onBack}>Back to teams</button>
      <h3>{org.name} / {team.name}</h3>
      <p className="subtle"><span className="mono">{team.key}</span> · {plural(team.members.length, "member")}</p>
      <Members org={org} team={team} users={users} admin={admin} />
      {admin && (
        <details>
          <summary>Team settings</summary>
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
            <label className="field">Team name
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required />
            </label>
            <button type="submit" disabled={rename.isPending || name.trim() === team.name}>Save name</button>
          </form>
          <p className="subtle">Renaming keeps its id, memberships and sharing rules.</p>
          <button type="button" className="danger" onClick={() => setDeleting(true)}>Delete team</button>
        </details>
      )}
      <ConfirmDialog open={deleting} title={`Delete ${team.name}?`} confirmLabel="Delete team" busy={remove.isPending}
        onCancel={() => setDeleting(false)} onConfirm={() => remove.mutate(undefined, { onSuccess: onBack })}>
        <p>Members stay in {org.name} and keep their accounts. Sessions shared with this team become private to their owners.</p>
      </ConfirmDialog>
    </section>
  );
}

function Members({ org, team, users, admin }: { org: Org; team?: Team; users: User[]; admin: boolean }) {
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const names = team ? team.members : org.members;
  const members = users.filter((user) => names.includes(user.name) && matches(query, user.name));
  const outside = users.filter((user) => !names.includes(user.name) && (!team || org.members.includes(user.name)));
  const membership = useDirectoryChange(({ name, member }: { name: string; member: boolean }) =>
    team ? api.setTeamMember(org.id, team.id, name, member) : api.setOrgMember(org.id, name, member));
  return (
    <div className="directory-members">
      <h4>{team ? "Team members" : "Organization members"}</h4>
      <p className="subtle">{team ? "Add people from this organization, or create an account here." : "Add existing users or create an account here, then place people in teams."}</p>
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search members" aria-label="Search members" />
      {members.length ? (
        <ul className="member-list">
          {members.map((user) => (
            <li key={user.name}>
              <div>
                <strong>{user.name}</strong>
                {user.role === "admin" && <span className="pill accent">Server admin</span>}
                {!team && <div className="row wrap">
                  {org.teams.filter((item) => item.members.includes(user.name)).map((item) => <span className="pill" key={item.key}>{item.name}</span>)}
                  {!user.teams.some((key) => key.startsWith(`${org.id}/`)) && <span className="subtle">No team yet</span>}
                </div>}
              </div>
              {admin && <button type="button" className="danger" disabled={membership.isPending} onClick={() => setRemoving(user.name)}>Remove member</button>}
            </li>
          ))}
        </ul>
      ) : <p className="subtle">{names.length ? "No members match." : "No members yet."}</p>}
      {admin && (
        <>
          {outside.length > 0 && (
            <form className="inline-form" onSubmit={(event: FormEvent) => {
              event.preventDefault();
              membership.mutate({ name: adding, member: true }, { onSuccess: () => setAdding("") });
            }}>
              <select value={adding} onChange={(event) => setAdding(event.target.value)} aria-label="Existing user" required>
                <option value="">Choose an existing user…</option>
                {outside.map((user) => <option key={user.name} value={user.name}>{user.name}</option>)}
              </select>
              <button type="submit" disabled={!adding || membership.isPending}>Add member</button>
            </form>
          )}
          {team && outside.length === 0 && <p className="subtle">To add another existing user, add them to the organization's Members first.</p>}
          <CreateUser key={team?.key ?? org.id} orgs={[org]} scope={{ org: org.id, team: team?.key }} />
        </>
      )}
      <ConfirmDialog open={removing !== null} title={`Remove ${removing ?? ""} from ${team?.name ?? org.name}?`}
        confirmLabel="Remove member" busy={membership.isPending} onCancel={() => setRemoving(null)}
        onConfirm={() => removing && membership.mutate({ name: removing, member: false }, { onSuccess: () => setRemoving(null) })}>
        <p>{team ? `They stay in ${org.name} and its other teams.` : "They also leave all teams in this organization."} Their account and memberships elsewhere stay. Access granted through this membership is removed.</p>
      </ConfirmDialog>
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
  const names = useMemo(() => new Map(orgs.map((item) => [item.id, item.name])), [orgs]);
  const shown = users.filter((user) => (!org || (org === "__none" ? !user.orgs.length : user.orgs.includes(org))) && matches(query, user.name, ...user.orgs, ...user.teams));
  return (
    <section className="section" aria-labelledby="users-heading">
      <div className="section-head">
        <h2 id="users-heading">User directory</h2>
        <span className="subtle">{admin ? `${users.length} can sign in` : "You and the people in your organizations"}</span>
      </div>
      <p className="subtle">Accounts belong to people, not to one team. Manage server roles and sign-in here; manage team members inside their organization.</p>
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search users" aria-label="Search users" />
        <select value={org} onChange={(event) => setOrg(event.target.value)} aria-label="Organization">
          <option value="">Every organization</option>
          <option value="__none">No organization</option>
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
              <th>Server role</th>
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
                busy={update.isPending}
                onChange={(body) => update.mutate({ name: user.name, ...body }, { onSuccess: () => setEditing(null) })}
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
      {admin && <CreateUser orgs={orgs} />}
      <ConfirmDialog
        open={removing !== null}
        title={`Delete ${removing?.name ?? ""}'s account?`}
        confirmLabel="Delete account"
        busy={remove.isPending}
        onCancel={() => setRemoving(null)}
        onConfirm={() => removing && remove.mutate(removing.name, { onSuccess: () => setRemoving(null) })}
      >
        <p>They're signed out everywhere and their launcher tokens stop working. Their sessions stay.</p>
      </ConfirmDialog>
    </section>
  );
}

function CreateUser({ orgs, scope }: { orgs: Org[]; scope?: { org: string; team?: string } }) {
  const blank = { name: "", password: "", role: "member", orgs: scope ? [scope.org] : [] as string[], teams: scope?.team ? [scope.team] : [] as string[] };
  const [draft, setDraft] = useState(blank);
  const add = useDirectoryChange(api.addUser);
  return (
    <details className="directory-create">
      <summary>Create user{scope ? " in this " + (scope.team ? "team" : "organization") : ""}</summary>
      <form className="directory-user-form" onSubmit={(event: FormEvent) => {
        event.preventDefault();
        add.mutate(draft, { onSuccess: () => setDraft(blank) });
      }}>
        <div className="form-grid two">
          <label className="field">Username
            <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} pattern={NAME_PATTERN} maxLength={40} autoCapitalize="none" autoComplete="off" required />
          </label>
          <label className="field">Temporary password
            <input type="password" value={draft.password} onChange={(event) => setDraft({ ...draft, password: event.target.value })} minLength={8} autoComplete="new-password" required />
          </label>
          {!scope && <label className="field">Server role
            <select value={draft.role} onChange={(event) => setDraft({ ...draft, role: event.target.value })}>
              <option value="member">Member</option>
              <option value="admin">Admin</option>
            </select>
          </label>}
        </div>
        {scope ? <p className="subtle">Creates a member account in {scope.team ?? scope.org}. They can change their password in Your account.</p> : (
          <fieldset className="field">
            <legend>Organizations and teams</legend>
            <Membership orgs={orgs} value={draft} onChange={(value) => setDraft({ ...draft, ...value })} />
          </fieldset>
        )}
        <button type="submit" className="primary" disabled={add.isPending}>Create user</button>
      </form>
    </details>
  );
}

function UserRow({
  user,
  orgs,
  names,
  identity,
  editing,
  onEdit,
  busy,
  onChange,
  onRemove,
}: {
  user: User;
  orgs: Org[];
  names: Map<string, string>;
  identity: Identity;
  editing: boolean;
  onEdit: () => void;
  busy: boolean;
  onChange: (body: { role?: string; orgs?: string[]; teams?: string[]; password?: string }) => void;
  onRemove: () => void;
}) {
  const admin = identity.role === "admin";
  const [membership, setMembership] = useState({ orgs: user.orgs, teams: user.teams });
  const [role, setRole] = useState(user.role);
  const [password, setPassword] = useState("");
  return (
    <>
      <tr className={`user-row${editing ? " selected" : ""}`}>
        <td>
          <strong>{user.name}</strong>
          {user.name === identity.user && <span className="subtle"> (you)</span>}
        </td>
        <td>
          {user.role === "admin" ? "Admin" : "Member"}
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
                  setRole(user.role);
                  setPassword("");
                  onEdit();
                }}
              >
                {editing ? "Cancel" : "Edit user"}
              </button>
              {user.name !== identity.user && (
                <button type="button" className="danger" onClick={onRemove}>
                  Delete account
                </button>
              )}
            </span>
          </td>
        )}
      </tr>
      {editing && (
        <tr className="user-edit-row selected">
          <td colSpan={4}>
            <form className="membership-edit" onSubmit={(event: FormEvent) => {
              event.preventDefault();
              onChange({ ...membership, role, ...(password ? { password } : {}) });
            }}>
              <div className="form-grid two">
                <label className="field">Server role
                  <select value={role} onChange={(event) => setRole(event.target.value === "admin" ? "admin" : "member")}>
                    <option value="member">Member</option>
                    <option value="admin">Admin</option>
                  </select>
                </label>
                <label className="field">Reset password (optional)
                  <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} minLength={8} autoComplete="new-password" />
                </label>
              </div>
              <Membership orgs={orgs} value={membership} onChange={setMembership} />
              <button type="submit" className="primary" disabled={busy}>
                Save {user.name}
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
