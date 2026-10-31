import { useMemo, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_ORIGIN, api, roleLabel, ROOT_ORG, type Directory, type Identity, type Org, type Team, type User } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PartTag } from "../components/PartTag";
import { TabBar, TabPanel, useTabParam } from "../components/Tabs";
import { useAuth, useClipboard, useStatus, useToast } from "../hooks";

const NAME_PATTERN = "[a-z0-9][a-z0-9._\\-]*";

/**
 * Who may change an organization: its own admins, and site admins (for any). Members only look.
 * Everything else about the rules is the server's; this only decides which controls to show.
 */
function manages(identity: Identity, org: string): boolean {
  return identity.site_admin || (identity.role === "admin" && identity.tenant === org);
}

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
          Every person belongs to one organization. Its sessions, machines and providers stay inside it. Teams group people within an
          organization for sharing sessions and machine access.
        </p>
      </header>
      {identity && (
        <>
          <TabBar
            label="Organization management"
            idPrefix="directory-"
            active={tab}
            onSelect={setTab}
            tabs={[{ id: "organizations", label: identity.site_admin ? "Organizations" : "Your organization" }, { id: "users", label: "People" }, { id: "account", label: "Your account" }]}
          />
          {(!orgs.data || !users.data) && !error && tab !== "account" && <p className="subtle">Loading directory…</p>}
          <TabPanel idPrefix="directory-" id="organizations" active={tab === "organizations"}>
            {orgs.data && users.data && <Organizations orgs={orgs.data.orgs} users={users.data.users} identity={identity} />}
          </TabPanel>
          <TabPanel idPrefix="directory-" id="users" active={tab === "users"}>
            {orgs.data && users.data && <Users orgs={orgs.data.orgs} users={users.data.users} identity={identity} />}
          </TabPanel>
          <TabPanel idPrefix="directory-" id="account" active={tab === "account"}>
            <YourAccount identity={identity} orgs={orgs.data?.orgs ?? []} />
          </TabPanel>
          {error && <p className="bad">{error.message}</p>}
        </>
      )}
    </div>
  );
}

function YourAccount({ identity, orgs }: { identity: Identity; orgs: Org[] }) {
  const copy = useClipboard();
  const status = useStatus();
  // The address this browser reached the server on works from other machines; the configured URL may be loopback.
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const server = API_ORIGIN || (loopback ? status.data?.proxy_url ?? window.location.origin : window.location.origin);
  const org = orgs.find((item) => item.id === identity.tenant);
  const role = identity.site_admin
    ? "Site admin: you manage the server's settings and set up organizations and their users. Other organizations' sessions and machines stay theirs."
    : identity.role === "admin"
      ? "Organization admin: you manage your organization's teams and people."
      : "Member.";
  return (
    <section className="panel" aria-labelledby="you-heading">
      <div className="panel-head">
        <h2 id="you-heading">You: {identity.user}</h2>
        <p className="subtle">
          {role} Organization: {org?.root ? "site administration" : org?.name ?? identity.tenant}.{" "}
          {identity.teams.length > 0 && `Teams: ${identity.teams.join(", ")}.`}
        </p>
      </div>
      <h3>Start agents from your own computer</h3>
      <p className="subtle">
        Run this command, then enter the code it shows on <Link to="/approve-device">Approve a device</Link> (or at the sign-in page it
        links to). The launcher renews its short-lived access tokens automatically.
      </p>
      <CodeLine text={`agent login ${server}`} onCopy={() => copy(`agent login ${server}`, "Command copied")} />
      <p className="subtle">For automation, an administrator provisions a service account at the issuer. API requests use a Bearer access token, never an ID token. Ask an admin of your organization to reset your password.</p>
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

/** An organization's name as people see it: root is the site administration. */
function orgLabel(org: Org | undefined, id: string): string {
  if (org?.root || id === ROOT_ORG) return "Site administration";
  return org?.name ?? id;
}

function Organizations({ orgs, users, identity }: { orgs: Org[]; users: User[]; identity: Identity }) {
  const siteAdmin = identity.site_admin;
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState({ id: "", name: "" });
  const [deleting, setDeleting] = useState<Org | null>(null);
  const add = useDirectoryChange(() => api.addOrg(draft.id.trim(), draft.name.trim() || draft.id.trim()));
  const remove = useDirectoryChange(api.removeOrg);
  const shown = orgs.filter((org) => matches(query, org.id, org.name, orgLabel(org, org.id), ...org.members));
  const selected = shown.find((org) => org.id === params.get("org")) ?? shown.find((org) => org.id === identity.tenant && !org.root) ?? shown[0] ?? null;
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
  // Someone outside site administration sees only their own organization: no list to choose from.
  if (!siteAdmin) {
    const own = orgs.find((org) => org.id === identity.tenant) ?? orgs[0];
    return own ? <OrgDetail key={own.id} org={own} users={users} identity={identity} onDelete={() => undefined} /> : <p className="subtle">Your organization isn't listed.</p>;
  }
  return (
    <section className="section" aria-labelledby="orgs-heading">
      <div className="section-head">
        <h2 id="orgs-heading">Organizations</h2>
        <span className="subtle">{orgs.filter((org) => !org.root).length} on this server</span>
      </div>
      <p className="subtle">
        Each organization is separate: its people, sessions, machines and providers aren't visible to the others. As a site admin you set
        them up and add their first users; their own admins take it from there.
      </p>
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search organizations or their people" aria-label="Search organizations" />
      </div>
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
        <p className="subtle">Then add its first admin under People, in the new organization.</p>
      </details>
      <div className="directory-workspace">
        <nav aria-label="Organizations">
          {shown.length ? (
            <ul className="org-list">
              {shown.map((org) => (
                <li key={org.id} className={`team-card org-card${selected?.id === org.id ? " selected" : ""}`}>
                  <button type="button" className="link org-open" aria-current={selected?.id === org.id ? "true" : undefined} onClick={() => select(org.id)}>
                    <strong>{orgLabel(org, org.id)}</strong>
                  </button>
                  <span className="mono subtle">{org.id}</span>
                  <span className="subtle">
                    {org.root ? plural(org.members.length, "site admin") : `${plural(org.members.length, "person", "people")} · ${plural(org.teams.length, "team")}`}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="subtle">{orgs.length ? "No organizations match." : "No organizations yet."}</p>
          )}
        </nav>
        {selected && <OrgDetail key={selected.id} org={selected} users={users} identity={identity} onDelete={() => setDeleting(selected)} />}
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
        <p>Its {plural(deleting?.teams.length ?? 0, "team")} go too. This can't be undone.</p>
      </ConfirmDialog>
    </section>
  );
}

function OrgDetail({ org, users, identity, onDelete }: { org: Org; users: User[]; identity: Identity; onDelete: () => void }) {
  const manager = manages(identity, org.id);
  const [name, setName] = useState(org.name);
  const [team, setTeam] = useState({ id: "", name: "" });
  // Root holds only site admins: no teams.
  const [tab, setTab] = useTabParam(org.root ? (["members", "settings"] as const) : (["teams", "members", "settings"] as const), "orgTab");
  const [params, setParams] = useSearchParams();
  const selected = org.teams.find((item) => item.id === params.get("team"));
  const people = users.filter((user) => user.tenant === org.id);
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
        <p className="subtle">
          {org.root ? "Site administration" : "Organization"} · <span className="mono">{org.id}</span>
          {org.id === identity.tenant && <> · yours</>}
        </p>
        <h2 id="org-detail-heading">{org.name}</h2>
        <p className="subtle">
          {org.root
            ? `${plural(people.length, "site admin")}. Site admins manage the server's settings and set up organizations; they don't see other organizations' sessions or machines.`
            : `${plural(org.teams.length, "team")} · ${plural(people.length, "person", "people")}`}
        </p>
      </header>
      <TabBar label={`${org.name} sections`} idPrefix="org-" active={tab} onSelect={setTab}
        tabs={[...(org.root ? [] : [{ id: "teams" as const, label: "Teams" }]), { id: "members" as const, label: org.root ? "Site admins" : "People" }, { id: "settings" as const, label: "Settings" }]} />
      {!org.root && (
        <TabPanel idPrefix="org-" id="teams" active={tab === "teams"}>
          {selected ? (
            <TeamDetail key={selected.key} org={org} team={selected} users={people} manager={manager} onBack={() => selectTeam(null)} />
          ) : (
            <>
              <p className="subtle">Teams group people in this organization. A person can be in more than one team.</p>
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
              {manager && (
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
      )}
      <TabPanel idPrefix="org-" id="members" active={tab === "members"}>
        <p className="subtle">
          {org.root
            ? "Only admins belong here. To add one, create a new user here."
            : "People belong to one organization, for good. To give someone access to this one, add them here as a new user."}
        </p>
        <UserTable users={people} orgs={[org]} identity={identity} showOrg={false} />
        {manager && <CreateUser key={org.id} orgs={[org]} fixed={org.id} />}
      </TabPanel>
      <TabPanel idPrefix="org-" id="settings" active={tab === "settings"}>
        <p className="subtle">The organization id stays the same when its name changes.</p>
        {manager ? (
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
            <label className="field">Organization name
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required />
            </label>
            <button type="submit" disabled={rename.isPending || name.trim() === org.name}>Save name</button>
          </form>
        ) : <p className="subtle">Only an admin of this organization can change it.</p>}
        {identity.site_admin && !org.root && (
          <div className="directory-danger">
            <h3>Delete organization</h3>
            <p className="subtle">
              {people.length
                ? `It still has ${plural(people.length, "person", "people")}. Delete their accounts first; an organization can only be deleted once it's empty.`
                : "It has no users left. Deleting it removes its teams too."}
            </p>
            <button type="button" className="danger" disabled={people.length > 0} onClick={onDelete}>Delete organization</button>
          </div>
        )}
      </TabPanel>
    </div>
  );
}

function TeamDetail({ org, team, users, manager, onBack }: { org: Org; team: Team; users: User[]; manager: boolean; onBack: () => void }) {
  const [name, setName] = useState(team.name);
  const [deleting, setDeleting] = useState(false);
  const rename = useDirectoryChange(() => api.renameTeam(org.id, team.id, name));
  const remove = useDirectoryChange(() => api.removeTeam(org.id, team.id));
  return (
    <section className="team-detail" aria-label={`${team.name} team`}>
      <button type="button" className="link" onClick={onBack}>Back to teams</button>
      <h3>{org.name} / {team.name}</h3>
      <p className="subtle"><span className="mono">{team.key}</span> · {plural(team.members.length, "member")}</p>
      <TeamMembers org={org} team={team} users={users} manager={manager} />
      {manager && (
        <details>
          <summary>Team settings</summary>
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
            <label className="field">Team name
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required />
            </label>
            <button type="submit" disabled={rename.isPending || name.trim() === team.name}>Save name</button>
          </form>
          <p className="subtle">Renaming keeps its id, members and sharing rules.</p>
          <button type="button" className="danger" onClick={() => setDeleting(true)}>Delete team</button>
        </details>
      )}
      <ConfirmDialog open={deleting} title={`Delete ${team.name}?`} confirmLabel="Delete team" busy={remove.isPending}
        onCancel={() => setDeleting(false)} onConfirm={() => remove.mutate(undefined, { onSuccess: onBack })}>
        <p>Its members stay in {org.name} and keep their accounts. Sessions shared with this team become private to their owners.</p>
      </ConfirmDialog>
    </section>
  );
}

/** A team's members, from the people in its organization (users: only theirs). */
function TeamMembers({ org, team, users, manager }: { org: Org; team: Team; users: User[]; manager: boolean }) {
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const members = users.filter((user) => team.members.includes(user.name) && matches(query, user.name));
  const outside = users.filter((user) => !team.members.includes(user.name));
  const membership = useDirectoryChange(({ name, member }: { name: string; member: boolean }) => api.setTeamMember(org.id, team.id, name, member));
  return (
    <div className="directory-members">
      <h4>Team members</h4>
      <p className="subtle">{manager ? "Add people from this organization, or create a new user in this team." : "Who is in this team."}</p>
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search members" aria-label="Search members" />
      {members.length ? (
        <ul className="member-list">
          {members.map((user) => (
            <li key={user.name}>
              <div>
                <strong>{user.name}</strong>
                {user.role === "admin" && <span className="pill accent">{roleLabel(user)}</span>}
              </div>
              {manager && <button type="button" className="danger" disabled={membership.isPending} onClick={() => setRemoving(user.name)}>Remove from team</button>}
            </li>
          ))}
        </ul>
      ) : <p className="subtle">{team.members.length ? "No members match." : "No members yet."}</p>}
      {manager && (
        <>
          {outside.length > 0 ? (
            <form className="inline-form" onSubmit={(event: FormEvent) => {
              event.preventDefault();
              membership.mutate({ name: adding, member: true }, { onSuccess: () => setAdding("") });
            }}>
              <select value={adding} onChange={(event) => setAdding(event.target.value)} aria-label="Person in this organization" required>
                <option value="">Choose someone in {org.name}…</option>
                {outside.map((user) => <option key={user.name} value={user.name}>{user.name}</option>)}
              </select>
              <button type="submit" disabled={!adding || membership.isPending}>Add to team</button>
            </form>
          ) : <p className="subtle">Everyone in {org.name} is in this team.</p>}
          <CreateUser key={team.key} orgs={[org]} fixed={org.id} team={team.key} />
        </>
      )}
      <ConfirmDialog open={removing !== null} title={`Remove ${removing ?? ""} from ${team.name}?`}
        confirmLabel="Remove from team" busy={membership.isPending} onCancel={() => setRemoving(null)}
        onConfirm={() => removing && membership.mutate({ name: removing, member: false }, { onSuccess: () => setRemoving(null) })}>
        <p>They stay in {org.name} and its other teams, and keep their account. Access granted through this team is removed.</p>
      </ConfirmDialog>
    </div>
  );
}

/** Ticks for the teams of one organization. */
function TeamPicker({ org, value, onChange }: { org: Org | undefined; value: string[]; onChange: (teams: string[]) => void }) {
  if (!org || org.root) return null;
  if (!org.teams.length) return <p className="subtle">{org.name} has no teams yet.</p>;
  const toggle = (key: string) => onChange(value.includes(key) ? value.filter((other) => other !== key) : [...value, key]);
  return (
    <fieldset className="field">
      <legend>Teams in {org.name}</legend>
      <span className="row wrap membership-teams">
        {org.teams.map((team) => (
          <label key={team.key} className="check">
            <input type="checkbox" checked={value.includes(team.key)} onChange={() => toggle(team.key)} />
            {team.name}
          </label>
        ))}
      </span>
    </fieldset>
  );
}

function Users({ orgs, users, identity }: { orgs: Org[]; users: User[]; identity: Identity }) {
  const siteAdmin = identity.site_admin;
  const [query, setQuery] = useState("");
  const [org, setOrg] = useState("");
  const names = useMemo(() => new Map(orgs.map((item) => [item.id, orgLabel(item, item.id)])), [orgs]);
  const shown = users.filter((user) => (!org || user.tenant === org) && matches(query, user.name, user.tenant, names.get(user.tenant) ?? "", ...user.teams));
  // Site admins add users to any organization; an organization's admins to their own.
  const addable = orgs.filter((item) => manages(identity, item.id));
  return (
    <section className="section" aria-labelledby="users-heading">
      <div className="section-head">
        <h2 id="users-heading">People</h2>
        <span className="subtle">{siteAdmin ? `${users.length} can sign in` : `The people in ${names.get(identity.tenant) ?? identity.tenant}`}</span>
      </div>
      <p className="subtle">
        People belong to one organization and can't be moved to another. To give someone access to an organization, add them there as a new user.
      </p>
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search people" aria-label="Search people" />
        {siteAdmin && (
          <select value={org} onChange={(event) => setOrg(event.target.value)} aria-label="Organization">
            <option value="">Every organization</option>
            {orgs.map((item) => (
              <option key={item.id} value={item.id}>
                {orgLabel(item, item.id)}
              </option>
            ))}
          </select>
        )}
      </div>
      <UserTable users={shown} orgs={orgs} identity={identity} showOrg={siteAdmin} />
      {addable.length > 0 && <CreateUser orgs={addable} fixed={siteAdmin ? undefined : identity.tenant} />}
    </section>
  );
}

/** People with their role, organization and teams; those who manage a person's organization can edit or delete them. */
function UserTable({ users, orgs, identity, showOrg }: { users: User[]; orgs: Org[]; identity: Identity; showOrg: boolean }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [removing, setRemoving] = useState<User | null>(null);
  const update = useDirectoryChange(({ name, ...body }: { name: string; role?: string; teams?: string[]; password?: string }) => api.updateUser(name, body));
  const remove = useDirectoryChange(api.removeUser);
  const anyManaged = users.some((user) => manages(identity, user.tenant));
  const columns = 3 + (showOrg ? 1 : 0) + (anyManaged ? 1 : 0);
  return (
    <>
      <div className="table-scroll">
        <table className="table users-table">
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              {showOrg && <th>Organization</th>}
              <th>Teams</th>
              {anyManaged && <th />}
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <UserRow
                key={user.name}
                user={user}
                org={orgs.find((item) => item.id === user.tenant)}
                identity={identity}
                showOrg={showOrg}
                columns={columns}
                manageable={anyManaged}
                editing={editing === user.name}
                onEdit={() => setEditing(editing === user.name ? null : user.name)}
                busy={update.isPending}
                onChange={(body) => update.mutate({ name: user.name, ...body }, { onSuccess: () => setEditing(null) })}
                onRemove={() => setRemoving(user)}
              />
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={columns} className="subtle">
                  No one here.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
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
    </>
  );
}

/** A new user in one organization: fixed names it, else the creator picks one of orgs (those they manage). */
function CreateUser({ orgs, fixed, team }: { orgs: Org[]; fixed?: string; team?: string }) {
  const initial = fixed ?? orgs.find((org) => !org.root)?.id ?? orgs[0]?.id ?? "";
  const blank = { name: "", password: "", role: "member", tenant: initial, teams: team ? [team] : ([] as string[]) };
  const [draft, setDraft] = useState(blank);
  const add = useDirectoryChange(api.addUser);
  const org = orgs.find((item) => item.id === draft.tenant);
  const root = Boolean(org?.root) || draft.tenant === ROOT_ORG;
  // Root holds only site admins.
  const role = root ? "admin" : draft.role;
  return (
    <details className="directory-create">
      <summary>{team ? "Create a user in this team" : fixed ? (root ? "Add a site admin" : "Add a person to this organization") : "Add a person"}</summary>
      <form className="directory-user-form" onSubmit={(event: FormEvent) => {
        event.preventDefault();
        add.mutate({ ...draft, role, teams: root ? [] : draft.teams }, { onSuccess: () => setDraft(blank) });
      }}>
        <div className="form-grid two">
          <label className="field">Username
            <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} pattern={NAME_PATTERN} maxLength={40} autoCapitalize="none" autoComplete="off" required />
          </label>
          <label className="field">Temporary password
            <input type="password" value={draft.password} onChange={(event) => setDraft({ ...draft, password: event.target.value })} minLength={8} autoComplete="new-password" required />
          </label>
          {!fixed && (
            <label className="field">Organization
              <select value={draft.tenant} onChange={(event) => setDraft({ ...draft, tenant: event.target.value, teams: [] })} required>
                {orgs.map((item) => (
                  <option key={item.id} value={item.id}>{orgLabel(item, item.id)}</option>
                ))}
              </select>
            </label>
          )}
          <label className="field">Role
            <select value={role} disabled={root} onChange={(event) => setDraft({ ...draft, role: event.target.value })}>
              {root ? <option value="admin">Site admin</option> : (
                <>
                  <option value="member">Member</option>
                  <option value="admin">Organization admin</option>
                </>
              )}
            </select>
          </label>
        </div>
        {team ? <p className="subtle">They join {team}. They can't be moved to another organization later.</p> : !root && <TeamPicker org={org} value={draft.teams} onChange={(teams) => setDraft({ ...draft, teams })} />}
        <p className="subtle">
          {root
            ? "A site admin manages the server's settings and sets up organizations. They don't see any organization's sessions or machines."
            : `They belong to ${org?.name ?? draft.tenant} for good. Give them the temporary password; they change it when they sign in.`}
        </p>
        <button type="submit" className="primary" disabled={add.isPending || !draft.tenant}>Create user</button>
      </form>
    </details>
  );
}

function UserRow({
  user,
  org,
  identity,
  showOrg,
  columns,
  manageable,
  editing,
  onEdit,
  busy,
  onChange,
  onRemove,
}: {
  user: User;
  org: Org | undefined;
  identity: Identity;
  showOrg: boolean;
  columns: number;
  /** Whether the table has an actions column. */
  manageable: boolean;
  editing: boolean;
  onEdit: () => void;
  busy: boolean;
  onChange: (body: { role?: string; teams?: string[]; password?: string }) => void;
  onRemove: () => void;
}) {
  const manager = manages(identity, user.tenant);
  const root = Boolean(org?.root) || user.tenant === ROOT_ORG;
  const [teams, setTeams] = useState(user.teams);
  const [role, setRole] = useState(user.role);
  const [password, setPassword] = useState("");
  return (
    <>
      <tr className={`user-row${editing ? " selected" : ""}`}>
        <td>
          <strong>{user.name}</strong>
          {user.name === identity.user && <span className="subtle"> (you)</span>}
        </td>
        <td>{roleLabel(user)}</td>
        {showOrg && (
          <td>
            <span className="pill accent" title={user.tenant}>{orgLabel(org, user.tenant)}</span>
          </td>
        )}
        <td>
          {user.teams.length ? (
            <span className="row wrap">
              {user.teams.map((key) => (
                <span key={key} className="pill">
                  {org?.teams.find((item) => item.key === key)?.name ?? key}
                </span>
              ))}
            </span>
          ) : (
            <span className="subtle">None</span>
          )}
        </td>
        {manageable && (
          <td>
            {manager && (
              <span className="row wrap">
                <button
                  type="button"
                  aria-expanded={editing}
                  onClick={() => {
                    setTeams(user.teams);
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
            )}
          </td>
        )}
      </tr>
      {editing && manager && (
        <tr className="user-edit-row selected">
          <td colSpan={columns}>
            <form className="membership-edit" onSubmit={(event: FormEvent) => {
              event.preventDefault();
              onChange({ ...(root ? {} : { role, teams }), ...(password ? { password } : {}) });
            }}>
              <div className="form-grid two">
                <label className="field">Role
                  <select value={root ? "admin" : role} disabled={root} onChange={(event) => setRole(event.target.value === "admin" ? "admin" : "member")}>
                    {root ? <option value="admin">Site admin</option> : (
                      <>
                        <option value="member">Member</option>
                        <option value="admin">Organization admin</option>
                      </>
                    )}
                  </select>
                </label>
                <label className="field">Reset password (optional)
                  <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} minLength={8} autoComplete="new-password" />
                </label>
              </div>
              {!root && <TeamPicker org={org} value={teams} onChange={setTeams} />}
              <p className="subtle">{user.name} stays in {orgLabel(org, user.tenant)}: people can't be moved between organizations.</p>
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

function plural(count: number, word: string, many = `${word}s`): string {
  return `${count} ${count === 1 ? word : many}`;
}
