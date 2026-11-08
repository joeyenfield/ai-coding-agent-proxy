import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_ORIGIN, api, roleLabel, ROOT_ORG, type Directory, type Identity, type Org, type Team, type User } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PartTag } from "../components/PartTag";
import { TabBar, TabCount, TabPanel, useTabParam } from "../components/Tabs";
import { OrgSharingTab } from "./OrgSharing";
import { ApiProviderSettings } from "./ApiProviders";
import { ApiCredentials } from "./ApiCredentials";
import { OrgDataTab } from "./OrgData";
import { OrgUsageTab } from "./OrgUsage";
import { useAuth, useClipboard, useStatus, useToast } from "../hooks";

const NAME_PATTERN = "[a-z0-9][a-z0-9._\\-]*";

/**
 * Who may change an organization: its own admins, and site admins (for any). Members only look.
 * Everything else about the rules is the server's; this only decides which controls to show.
 */
function manages(identity: Identity, org: string): boolean {
  return identity.site_admin || (identity.role === "admin" && identity.tenant === org);
}

/** An id from a display name, as the server accepts ids: lowercase letters, digits, dots, dashes and underscores. */
function idFrom(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "").replace(/-+$/, "").slice(0, 40);
}

export function OrganizationPage() {
  const auth = useAuth();
  const identity = auth.data?.identity;
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs, enabled: Boolean(identity) });
  const users = useQuery({ queryKey: ["users"], queryFn: api.users, enabled: Boolean(identity) });
  // Everyone but site admins has one organization, whose People tab already lists everyone they can see.
  const siteAdmin = Boolean(identity?.site_admin);
  const [tab, setTab] = useTabParam(siteAdmin ? (["organizations", "users", "account"] as const) : (["organizations", "account"] as const));
  const error = orgs.error ?? users.error;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>{siteAdmin ? "Organizations" : "Your organization"}</h1>
        <p className="lede">
          {siteAdmin
            ? "Each organization is a separate space: its people, sessions, machines and providers stay inside it. Teams are smaller groups within one."
            : "The people, teams and settings of your organization. Your sessions, machines and providers stay inside it."}
        </p>
      </header>
      {identity && (
        <>
          <TabBar
            label="Organizations"
            idPrefix="directory-"
            active={tab}
            onSelect={setTab}
            tabs={[
              { id: "organizations", label: siteAdmin ? "Organizations" : "Your organization" },
              ...(siteAdmin ? [{ id: "users" as const, label: "Everyone" }] : []),
              { id: "account", label: "Your account" },
            ]}
          />
          {(!orgs.data || !users.data) && !error && tab !== "account" && <p className="subtle">Loading…</p>}
          <TabPanel idPrefix="directory-" id="organizations" active={tab === "organizations"}>
            {orgs.data && users.data && <Organizations orgs={orgs.data.orgs} users={users.data.users} identity={identity} />}
          </TabPanel>
          {siteAdmin && (
            <TabPanel idPrefix="directory-" id="users" active={tab === "users"}>
              {orgs.data && users.data && <Users orgs={orgs.data.orgs} users={users.data.users} identity={identity} />}
            </TabPanel>
          )}
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

/** A name and an id for something new: the id follows the name until it's edited, and never changes afterwards. */
function NewNameForm({ kind, example, busy, onCreate, onCancel }: {
  kind: string;
  example: string;
  busy: boolean;
  onCreate: (id: string, name: string, done: () => void) => void;
  onCancel?: () => void;
}) {
  const [name, setName] = useState("");
  const [id, setId] = useState("");
  const [edited, setEdited] = useState(false);
  const effective = edited ? id.trim() : idFrom(name);
  return (
    <form
      className="panel org-new"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        onCreate(effective.toLowerCase(), name.trim() || effective, () => {
          setName("");
          setId("");
          setEdited(false);
        });
      }}
    >
      <h3>New {kind}</h3>
      <div className="form-grid two">
        <label className="field">Name
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder={`Such as ${example}`} maxLength={80} required />
          <small className="subtle">What people see. You can rename it later.</small>
        </label>
        <label className="field">ID
          <input
            className="mono"
            value={edited ? id : idFrom(name)}
            onChange={(event) => (setEdited(true), setId(event.target.value))}
            placeholder={idFrom(example)}
            pattern={NAME_PATTERN}
            maxLength={40}
            autoCapitalize="none"
            required
          />
          <small className="subtle">Used in commands. It can't be changed later.</small>
        </label>
      </div>
      <div className="row">
        <button type="submit" className="primary" disabled={busy || !effective}>Create {kind}</button>
        {onCancel && <button type="button" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}

function Organizations({ orgs, users, identity }: { orgs: Org[]; users: User[]; identity: Identity }) {
  const siteAdmin = identity.site_admin;
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [deleting, setDeleting] = useState<Org | null>(null);
  const add = useDirectoryChange(({ id, name }: { id: string; name: string }) => api.addOrg(id, name));
  const remove = useDirectoryChange(api.removeOrg);
  const [creating, setCreating] = useState(false);
  const shown = orgs.filter((org) => matches(query, org.id, org.name, orgLabel(org, org.id), ...org.members));
  // A site admin starts at the list of organizations and opens one; ?org= keeps it open across reloads.
  const selected = orgs.find((org) => org.id === params.get("org")) ?? null;
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
      {selected ? (
        <>
          <nav className="org-switch" aria-label="Organizations">
            <span className="breadcrumbs">
              <button type="button" className="link" onClick={() => select(null)}>All organizations</button>
              <span className="subtle" aria-hidden="true">›</span>
              <span>{orgLabel(selected, selected.id)}</span>
            </span>
            <label className="org-switch-pick">
              <span className="subtle">Go to</span>
              <select value={selected.id} onChange={(event) => select(event.target.value)} aria-label="Go to organization">
                {orgs.map((org) => <option key={org.id} value={org.id}>{orgLabel(org, org.id)}</option>)}
              </select>
            </label>
          </nav>
          <OrgDetail key={selected.id} org={selected} users={users} identity={identity} onDelete={() => setDeleting(selected)} />
        </>
      ) : (
        <>
          <div className="filters org-toolbar">
            <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search organizations or their people" aria-label="Search organizations" />
            {!creating && <button type="button" className="primary" onClick={() => setCreating(true)}>New organization</button>}
          </div>
          {creating && (
            <NewNameForm
              kind="organization"
              example="Acme Labs"
              busy={add.isPending}
              onCancel={() => setCreating(false)}
              onCreate={(id, name, done) =>
                add.mutate({ id, name }, {
                  onSuccess: () => {
                    done();
                    setQuery("");
                    setCreating(false);
                    select(id);
                  },
                })
              }
            />
          )}
          {shown.length ? (
            <div className="table-scroll">
              <table className="table org-table">
                <thead>
                  <tr>
                    <th>Organization</th>
                    <th>People</th>
                    <th>Teams</th>
                    <th>Admins</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((org) => {
                    const people = users.filter((user) => user.tenant === org.id);
                    const admins = people.filter((user) => user.role === "admin").map((user) => user.name);
                    return (
                      <tr key={org.id} className="org-row" onClick={() => select(org.id)}>
                        <td>
                          <button type="button" className="link org-open" onClick={(event) => (event.stopPropagation(), select(org.id))}>
                            {orgLabel(org, org.id)}
                          </button>
                          {org.id === identity.tenant && <span className="pill">yours</span>}
                          <div className="subtle org-row-note">{org.root ? "Site admins only" : <>ID <span className="mono">{org.id}</span></>}</div>
                        </td>
                        <td>{people.length}</td>
                        <td>{org.root ? <span className="subtle">—</span> : org.teams.length}</td>
                        <td>{admins.length ? admins.join(", ") : <span className="pill bad">No admin yet</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="subtle">{orgs.length ? "No organizations match." : "No organizations yet."}</p>
          )}
        </>
      )}
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

/** A tab's heading and what it's for, so each tab says what it holds before showing it. */
function TabIntro({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="org-tab-intro">
      <h3>{title}</h3>
      <p className="subtle">{children}</p>
    </div>
  );
}

type OrgTab = "members" | "teams" | "providers" | "credentials" | "usage" | "data" | "sharing" | "settings";

function OrgDetail({ org, users, identity, onDelete }: { org: Org; users: User[]; identity: Identity; onDelete: () => void }) {
  const manager = manages(identity, org.id);
  const [name, setName] = useState(org.name);
  const label = orgLabel(org, org.id);
  // Root holds only site admins: no teams, no sharing, and its name is fixed.
  const tabs: { id: OrgTab; label: string; badge?: ReactNode }[] = [
    { id: "members", label: org.root ? "Site admins" : "People", badge: <TabCount value={users.filter((user) => user.tenant === org.id).length} /> },
    ...(org.root ? [] : [{ id: "teams" as const, label: "Teams", badge: <TabCount value={org.teams.length} /> }]),
    ...(manager ? [{ id: "providers" as const, label: "API providers" }] : []),
    ...(manager ? [{ id: "credentials" as const, label: "API credentials" }] : []),
    { id: "usage", label: "Usage" },
    ...(manager ? [{ id: "data" as const, label: "Data" }] : []),
    ...(manager && !org.root ? [{ id: "sharing" as const, label: "Sharing" }] : []),
    ...(org.root ? [] : [{ id: "settings" as const, label: "Settings" }]),
  ];
  const [tab, setTab] = useTabParam(tabs.map((item) => item.id), "orgTab");
  const [params, setParams] = useSearchParams();
  const selected = org.teams.find((item) => item.id === params.get("team"));
  const people = users.filter((user) => user.tenant === org.id);
  const admins = people.filter((user) => user.role === "admin");
  const rename = useDirectoryChange(() => api.renameOrg(org.id, name));
  const addTeam = useDirectoryChange(({ id, name }: { id: string; name: string }) => api.addTeam(org.id, id, name));
  const [creatingTeam, setCreatingTeam] = useState(false);
  // "Add an admin" opens the add-a-person form with the admin role chosen; the key remounts it that way.
  const [addingAdmin, setAddingAdmin] = useState(false);
  const wantsAdmin = addingAdmin && admins.length === 0;
  const selectTeam = (id: string | null) => setParams((current) => {
    const next = new URLSearchParams(current);
    if (id) next.set("team", id);
    else next.delete("team");
    // Pin the organization even when it was selected by default.
    next.set("org", org.id);
    next.set("orgTab", "teams");
    return next;
  }, { replace: true });
  return (
    <div className="org-detail">
      <header className="org-detail-head">
        <h2 id="org-detail-heading">
          {label}
          {org.id === identity.tenant && <span className="pill">yours</span>}
        </h2>
        <dl className="org-facts">
          {!org.root && (
            <div>
              <dt>ID</dt>
              <dd className="mono">{org.id}</dd>
            </div>
          )}
          <div>
            <dt>{org.root ? "Site admins" : "People"}</dt>
            <dd>{people.length}</dd>
          </div>
          {!org.root && (
            <>
              <div>
                <dt>Teams</dt>
                <dd>{org.teams.length}</dd>
              </div>
              <div>
                <dt>Admins</dt>
                <dd>{admins.length ? admins.map((user) => user.name).join(", ") : "None yet"}</dd>
              </div>
            </>
          )}
        </dl>
        {org.root && (
          <p className="subtle">Site admins manage the server's settings and set up organizations. They don't see other organizations' sessions or machines.</p>
        )}
      </header>
      {!org.root && manager && admins.length === 0 && (
        <div className="callout row">
          <span>No one can manage {label} yet. Add a person with the organization admin role; they take it from there.</span>
          <button type="button" className="primary" onClick={() => (setTab("members"), setAddingAdmin(true))}>Add an admin</button>
        </div>
      )}
      <TabBar label={`${label} sections`} idPrefix="org-" active={tab} onSelect={setTab} tabs={tabs} />
      <TabPanel idPrefix="org-" id="members" active={tab === "members"}>
        <TabIntro title={org.root ? "Site admins" : "People"}>
          {org.root
            ? "Only site admins belong here. To add one, create them here."
            : "Everyone who signs in to this organization. People belong to one organization for good: to give someone access, create their account here."}
        </TabIntro>
        <UserTable users={people} orgs={[org]} identity={identity} showOrg={false} />
        {manager && <CreateUser key={`${org.id}${wantsAdmin ? "-admin" : ""}`} orgs={[org]} fixed={org.id} admin={wantsAdmin} />}
      </TabPanel>
      {!org.root && (
        <TabPanel idPrefix="org-" id="teams" active={tab === "teams"}>
          {selected ? (
            <TeamDetail key={selected.key} org={org} team={selected} users={people} manager={manager} onBack={() => selectTeam(null)} />
          ) : (
            <>
              <TabIntro title="Teams">
                Smaller groups within {label}, for sharing sessions and giving access to machines. A person can be in several teams.
              </TabIntro>
              {org.teams.length ? (
                <ul className="team-list">
                  {org.teams.map((item) => (
                    <li key={item.key} className="team-card">
                      <button type="button" className="link org-open" onClick={() => selectTeam(item.id)}>{item.name}</button>
                      <span className="subtle">{plural(item.members.length, "member")} · ID <span className="mono">{item.id}</span></span>
                    </li>
                  ))}
                </ul>
              ) : <p className="subtle">No teams yet.</p>}
              {manager && (creatingTeam ? (
                <NewNameForm
                  kind="team"
                  example="Core platform"
                  busy={addTeam.isPending}
                  onCancel={() => setCreatingTeam(false)}
                  onCreate={(id, name, done) => addTeam.mutate({ id, name }, { onSuccess: () => (done(), setCreatingTeam(false), selectTeam(id)) })}
                />
              ) : (
                <div><button type="button" onClick={() => setCreatingTeam(true)}>New team</button></div>
              ))}
            </>
          )}
        </TabPanel>
      )}
      {manager && (
        <TabPanel idPrefix="org-" id="providers" active={tab === "providers"}>
          <ApiProviderSettings org={org} />
        </TabPanel>
      )}
      <TabPanel idPrefix="org-" id="usage" active={tab === "usage"}>
        <OrgUsageTab org={org.id} />
      </TabPanel>
      {manager && <TabPanel idPrefix="org-" id="credentials" active={tab === "credentials"}><ApiCredentials org={org.id} users={people} /></TabPanel>}
      {manager && (
        <TabPanel idPrefix="org-" id="data" active={tab === "data"}>
          <OrgDataTab org={org.id} people={people.map((user) => user.name)} />
        </TabPanel>
      )}
      {manager && !org.root && (
        <TabPanel idPrefix="org-" id="sharing" active={tab === "sharing"}>
          <OrgSharingTab org={org} />
        </TabPanel>
      )}
      {!org.root && (
        <TabPanel idPrefix="org-" id="settings" active={tab === "settings"}>
          <section className="org-setting">
            <TabIntro title="Name">
              The name people see in the console. Its ID, <span className="mono">{org.id}</span>, never changes: commands, team keys and sharing use it.
            </TabIntro>
            {manager ? (
              <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
                <label className="field">Name
                  <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required />
                </label>
                <button type="submit" disabled={rename.isPending || name.trim() === org.name}>Save name</button>
              </form>
            ) : <p className="subtle">Only an admin of this organization can change it.</p>}
          </section>
          {identity.site_admin && (
            <section className="org-setting directory-danger">
              <TabIntro title="Delete organization">
                {people.length
                  ? `It still has ${plural(people.length, "person", "people")}. Delete their accounts on the People tab first: only an empty organization can be deleted.`
                  : "It has no one left. Deleting it removes its teams too. This can't be undone."}
              </TabIntro>
              <div><button type="button" className="danger" disabled={people.length > 0} onClick={onDelete}>Delete organization</button></div>
            </section>
          )}
        </TabPanel>
      )}
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
      <span className="breadcrumbs">
        <button type="button" className="link" onClick={onBack}>Teams</button>
        <span className="subtle" aria-hidden="true">›</span>
        <span>{team.name}</span>
      </span>
      <div className="org-tab-intro">
        <h3>{team.name}</h3>
        <p className="subtle">{plural(team.members.length, "member")} · ID <span className="mono">{team.id}</span></p>
      </div>
      <TeamMembers org={org} team={team} users={users} manager={manager} />
      {manager && (
        <details className="directory-create">
          <summary>Rename or delete this team</summary>
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), rename.mutate(undefined))}>
            <label className="field">Name
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={80} required />
            </label>
            <button type="submit" disabled={rename.isPending || name.trim() === team.name}>Save name</button>
          </form>
          <p className="subtle">Renaming keeps its ID, members and sharing rules.</p>
          <div><button type="button" className="danger" onClick={() => setDeleting(true)}>Delete team</button></div>
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
      <h4>Members</h4>
      {manager && <p className="subtle">Add people who are already in {org.name}, or create a new account straight into this team.</p>}
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
      <div className="org-tab-intro">
        <h2 id="users-heading">Everyone on this server</h2>
        <p className="subtle">
          {plural(users.length, "person", "people")} in every organization. Each person belongs to one organization for good: to give someone
          access to another, create an account for them there.
        </p>
      </div>
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
function CreateUser({ orgs, fixed, team, admin = false }: { orgs: Org[]; fixed?: string; team?: string; admin?: boolean }) {
  const initial = fixed ?? orgs.find((org) => !org.root)?.id ?? orgs[0]?.id ?? "";
  const blank = { name: "", password: "", role: admin ? "admin" : "member", tenant: initial, teams: team ? [team] : ([] as string[]) };
  const [draft, setDraft] = useState(blank);
  const add = useDirectoryChange(api.addUser);
  const org = orgs.find((item) => item.id === draft.tenant);
  const root = Boolean(org?.root) || draft.tenant === ROOT_ORG;
  // Root holds only site admins.
  const role = root ? "admin" : draft.role;
  return (
    <details className="directory-create" open={admin || undefined}>
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
