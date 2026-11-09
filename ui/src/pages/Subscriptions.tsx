import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type AccountState,
  type AccountUsage,
  type DeclaredLimit,
  type ProviderAccount,
  type ProviderAccountList,
  type SubscriptionProvider,
  type UsageWindow,
} from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PartTag } from "../components/PartTag";
import { StateDot } from "../components/ProviderState";
import { useClipboard, useMachines, useToast } from "../hooks";

const ENROLL = "agent accounts enroll claude";

/** How each availability state reads, and its dot. */
export const STATE_LABELS: Record<AccountState, { text: string; tone: "good" | "bad" | "live" | "idle" }> = {
  available: { text: "Available", tone: "good" },
  near_limit: { text: "Near its limit", tone: "live" },
  unknown: { text: "Sign-in unknown", tone: "idle" },
  rate_limited: { text: "Rate limited", tone: "live" },
  quota_exhausted: { text: "Limit used up", tone: "bad" },
  auth_expired: { text: "Needs signing in", tone: "bad" },
  offline: { text: "No machine ready", tone: "idle" },
};

const WINDOW_LABELS: Record<UsageWindow, string> = { "5h": "Last 5 hours", "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days", month: "This month (UTC)" };

const when = (at: number | null | undefined) => (at ? new Date(at * 1000).toLocaleString() : "never");

export function AccountStateBadge({ account }: { account: Pick<ProviderAccount, "state" | "state_reason" | "status"> }) {
  if (account.status === "disabled") return <span className="pill">Turned off</span>;
  const state = STATE_LABELS[account.state];
  return (
    <span className="row" title={account.state_reason}>
      <StateDot tone={state.tone} /> {state.text}
    </span>
  );
}

/**
 * Subscriptions: people's own AI plans (Claude, ChatGPT through Codex, GitHub Copilot), apart from their
 * sign-in to this server. Each runs on the machines its owner assigns it to; a stored Copilot token is
 * handed to those machines' launches, Claude Code and Codex stay signed in on each machine.
 */
export function SubscriptionsPage() {
  const accounts = useQuery({ queryKey: ["provider-accounts"], queryFn: () => api.providerAccounts(), refetchInterval: 10000 });
  const [adding, setAdding] = useState(false);
  const copy = useClipboard();
  const list = accounts.data;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Subscriptions</h1>
        <p className="lede">
          Your own AI plans: Claude, ChatGPT (through Codex) and GitHub Copilot. Agents started on a machine you assign a subscription to run
          with it, and its limits stay the provider's. This isn't your sign-in to this server: that's on <Link to="/organization?tab=account">Your account</Link>.
        </p>
      </header>
      {accounts.isError && <p className="bad">{(accounts.error as Error).message}</p>}
      {list && (
        <>
          <div className="row wrap">
            <button type="button" className="primary" onClick={() => setAdding(!adding)}>
              {adding ? "Close" : "Add a subscription"}
            </button>
          </div>
          {adding && <AddAccount list={list} onDone={() => setAdding(false)} />}
          {list.accounts.length === 0 && !adding ? (
            <div className="empty panel">
              <p>No subscriptions yet.</p>
              <p className="subtle">Sign in with the provider's CLI on a machine, then run this there:</p>
              <CodeLine text={ENROLL} onCopy={() => copy(ENROLL, "Command copied")} />
            </div>
          ) : (
            list.accounts.map((account) => <AccountCard key={account.id} account={account} list={list} />)
          )}
        </>
      )}
    </div>
  );
}

function AddAccount({ list, onDone }: { list: ProviderAccountList; onDone: () => void }) {
  const notify = useToast();
  const client = useQueryClient();
  const [provider, setProvider] = useState<SubscriptionProvider>("claude");
  const [name, setName] = useState("");
  const [plan, setPlan] = useState("");
  const [mode, setMode] = useState<"runner" | "token">("runner");
  const info = list.providers[provider];
  const create = useMutation({
    mutationFn: () => api.createProviderAccount({ provider, display_name: name || undefined, plan_name: plan || undefined, authentication_mode: mode }),
    onSuccess: (account) => {
      notify(account.existing ? `You already have ${account.display_name}.` : `Added ${account.display_name}.`);
      client.invalidateQueries({ queryKey: ["provider-accounts"] });
      onDone();
    },
    onError: (error: Error) => notify(error.message),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };
  return (
    <form className="panel" onSubmit={submit}>
      <div className="form-grid">
        <label className="field">
          Provider
          <select
            value={provider}
            onChange={(event) => {
              const next = event.target.value as SubscriptionProvider;
              setProvider(next);
              setMode("runner");
              setPlan("");
            }}
          >
            {(Object.keys(list.providers) as SubscriptionProvider[]).map((key) => (
              <option key={key} value={key}>
                {list.providers[key].label}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Plan <span className="optional">optional</span>
          <select value={plan} onChange={(event) => setPlan(event.target.value)}>
            <option value="">Not said</option>
            {info.plans.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="field span-2">
          Name <span className="optional">optional</span>
          <input value={name} placeholder={`Your ${info.label.split(" (")[0]}`} onChange={(event) => setName(event.target.value)} />
        </label>
        <fieldset className="field span-2">
          <legend>How it signs in</legend>
          {info.modes.map((item) => (
            <label key={item} className="check">
              <input type="radio" name="mode" checked={mode === item} onChange={() => setMode(item)} />
              {list.modes[item]}
            </label>
          ))}
        </fieldset>
      </div>
      <p className="subtle">{mode === "token" ? info.note : info.sign_in}</p>
      {mode === "token" && !list.vault.configured && (
        <p className="bad">This server can't store tokens yet: its administrator sets AI_PROXY_VAULT_KEYS (agent-server vault key makes one).</p>
      )}
      <div className="row wrap">
        <button type="submit" className="primary" disabled={create.isPending}>
          {create.isPending ? "Adding…" : "Add"}
        </button>
        <button type="button" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function AccountCard({ account, list }: { account: ProviderAccount; list: ProviderAccountList }) {
  const [open, setOpen] = useState(false);
  const info = list.providers[account.provider];
  return (
    <section className="panel" aria-labelledby={`account-${account.id}`}>
      <div className="row wrap account-head">
        <div>
          <h2 id={`account-${account.id}`}>{account.display_name}</h2>
          <p className="subtle">
            {account.provider_label}
            {account.plan_name && ` · ${account.plan_name}`} · {account.mode_label}
            {!account.you.owner && ` · ${account.owner}'s`}
          </p>
        </div>
        <AccountStateBadge account={account} />
      </div>
      <p className="subtle">{account.state_reason}</p>
      <dl className="machine-facts">
        <div>
          <dt>Machines</dt>
          <dd>
            {account.assignments.length === 0
              ? "None yet"
              : account.assignments.map((item) => (
                  <span key={item.machine_id} className="row" title={item.health.error_message ?? "Ready"}>
                    <StateDot tone={item.health.available ? "good" : "idle"} />
                    <Link to={`/machines/${encodeURIComponent(item.machine_id)}/accounts`}>{item.health.machine_name ?? item.machine_id}</Link>
                    {!item.health.available && <span className="subtle"> {item.health.error_message}</span>}
                  </span>
                ))}
          </dd>
        </div>
        {account.authentication_mode === "token" && (
          <div>
            <dt>Token</dt>
            <dd>{account.credential.stored ? `Stored, encrypted, ${when(account.credential.set_at)}` : "None stored"}</dd>
          </div>
        )}
        <div>
          <dt>Last answer</dt>
          <dd>{when(account.last_success_at)}</dd>
        </div>
      </dl>
      <button type="button" className="small" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Hide details" : "Usage and settings"}
      </button>
      {open && <AccountDetails account={account} info={info} vault={list.vault.configured} />}
    </section>
  );
}

function AccountDetails({ account, info, vault }: { account: ProviderAccount; info: ProviderAccountList["providers"][SubscriptionProvider]; vault: boolean }) {
  const detail = useQuery({ queryKey: ["provider-account", account.id], queryFn: () => api.providerAccount(account.id), refetchInterval: 15000 });
  const usage = detail.data?.usage;
  return (
    <div className="stack">
      {usage ? <UsagePanel usage={usage} account={account} /> : <p className="subtle">Loading usage…</p>}
      {account.you.manage && <Assign account={account} />}
      {account.you.manage && account.authentication_mode === "token" && <TokenPanel account={account} info={info} vault={vault} />}
      {account.you.manage && <AccountSettings account={account} windows={info.windows} />}
      {!account.you.manage && account.you.suspend && <Suspend account={account} />}
      {account.you.suspend && <Events account={account} />}
    </div>
  );
}

function UsagePanel({ usage, account }: { usage: AccountUsage; account: ProviderAccount }) {
  const estimate = usage.estimate;
  const limited = usage.rate_limits;
  return (
    <div className="stack">
      <h3>Usage</h3>
      <p className="subtle">
        What went through this server for {account.display_name}. The provider's own meter isn't available to the server, so use on other computers or
        in the provider's apps isn't counted.
      </p>
      <div className="meter-grid">
        {usage.observed
          .filter((item) => item.window !== "30d")
          .map((item) => (
            <div className="meter" key={item.window}>
              <div className="meter-head">
                <span>{WINDOW_LABELS[item.window]}</span>
                <strong>{item.requests.toLocaleString()} requests</strong>
              </div>
              <span className="subtle">
                {(item.input_tokens + item.output_tokens).toLocaleString()} tokens{item.rate_limited ? ` · ${item.rate_limited} refused for limits` : ""}
              </span>
            </div>
          ))}
      </div>
      <h3>Remaining</h3>
      {estimate ? (
        <div className="meter">
          <div className="meter-head">
            <span>
              About {estimate.remaining.toLocaleString()} of {estimate.limit.value.toLocaleString()} {estimate.limit.unit} left ({WINDOW_LABELS[estimate.limit.window].toLowerCase()})
            </span>
            <strong>{estimate.percent_used ?? 0}% used</strong>
          </div>
          <div className="meter-bar" aria-hidden>
            <span className={estimate.warning ? "high" : undefined} style={{ width: `${Math.min(100, estimate.percent_used ?? 0)}%` }} />
          </div>
          <span className="subtle">Estimated. {estimate.basis}</span>
        </div>
      ) : (
        <p>
          Unknown. <span className="subtle">Declare the plan's limit below to get an estimate from what goes through this server.</span>
        </p>
      )}
      {(limited.cooldown_until || limited.exhausted_until || limited.last_limited_at) && (
        <p className={limited.exhausted_until || limited.cooldown_until ? "warn" : "subtle"}>
          {limited.exhausted_until
            ? `The provider said its limit is used up until ${when(limited.exhausted_until)}.`
            : limited.cooldown_until
              ? `The provider is throttling it until ${when(limited.cooldown_until)}.`
              : `The provider last refused a request for limits ${when(limited.last_limited_at)}.`}{" "}
          Seen in its replies.
        </p>
      )}
    </div>
  );
}

function Assign({ account }: { account: ProviderAccount }) {
  const notify = useToast();
  const client = useQueryClient();
  const machines = useMachines();
  const assigned = new Set(account.assignments.map((item) => item.machine_id));
  // Only your own machines can run your subscription.
  const mine = (machines.data?.machines ?? []).filter((machine) => machine.you.owner && !assigned.has(machine.id));
  const [machine, setMachine] = useState("");
  const done = () => {
    client.invalidateQueries({ queryKey: ["provider-accounts"] });
    client.invalidateQueries({ queryKey: ["provider-account", account.id] });
  };
  const assign = useMutation({
    mutationFn: (id: string) => api.assignProviderAccount(account.id, id),
    onSuccess: () => {
      setMachine("");
      done();
    },
    onError: (error: Error) => notify(error.message),
  });
  const unassign = useMutation({ mutationFn: (id: string) => api.unassignProviderAccount(account.id, id), onSuccess: done, onError: (error: Error) => notify(error.message) });
  const provisioning = useMutation({
    mutationFn: ({ id, allowed }: { id: string; allowed: boolean }) => api.assignProviderAccount(account.id, id, { provisioning_allowed: allowed }),
    onSuccess: done,
    onError: (error: Error) => notify(error.message),
  });
  return (
    <div className="stack">
      <h3>Machines</h3>
      {account.assignments.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Machine</th>
              <th>Ready</th>
              {account.authentication_mode === "token" && <th>Gets the token</th>}
              <th />
            </tr>
          </thead>
          <tbody>
            {account.assignments.map((item) => (
              <tr key={item.machine_id}>
                <td>{item.health.machine_name ?? item.machine_id}</td>
                <td className={item.health.available ? "good" : "subtle"}>{item.health.available ? (item.health.authenticated === null ? "Probably" : "Yes") : item.health.error_message}</td>
                {account.authentication_mode === "token" && (
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Hand the token to launches on ${item.health.machine_name ?? item.machine_id}`}
                      checked={item.provisioning_allowed}
                      onChange={(event) => provisioning.mutate({ id: item.machine_id, allowed: event.target.checked })}
                    />
                  </td>
                )}
                <td>
                  <button type="button" className="small" onClick={() => unassign.mutate(item.machine_id)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {mine.length > 0 ? (
        <div className="row wrap">
          <select value={machine} onChange={(event) => setMachine(event.target.value)} aria-label="Machine to assign">
            <option value="">Choose one of your machines…</option>
            {mine.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <button type="button" disabled={!machine || assign.isPending} onClick={() => assign.mutate(machine)}>
            Assign
          </button>
        </div>
      ) : (
        <p className="subtle">
          {account.assignments.length ? "It's on all your machines." : "You have no machines yet."} Or run <code>agent accounts enroll {account.provider}</code> on one.
        </p>
      )}
    </div>
  );
}

function TokenPanel({ account, info, vault }: { account: ProviderAccount; info: ProviderAccountList["providers"][SubscriptionProvider]; vault: boolean }) {
  const notify = useToast();
  const client = useQueryClient();
  const [token, setToken] = useState("");
  const [revoking, setRevoking] = useState(false);
  const done = () => {
    client.invalidateQueries({ queryKey: ["provider-accounts"] });
    client.invalidateQueries({ queryKey: ["provider-account", account.id] });
  };
  const store = useMutation({
    mutationFn: () => api.storeProviderToken(account.id, token.trim()),
    onSuccess: () => {
      // The browser doesn't keep it: the field is cleared and the server never sends it back.
      setToken("");
      notify("Stored, encrypted.");
      done();
    },
    onError: (error: Error) => notify(error.message),
  });
  const revoke = useMutation({
    mutationFn: () => api.revokeProviderToken(account.id),
    onSuccess: () => {
      notify("Forgotten here. Revoke it on GitHub too.");
      done();
    },
    onError: (error: Error) => notify(error.message),
  });
  return (
    <div className="stack">
      <h3>Token</h3>
      <p className="subtle">
        {info.note} Use a fine-grained personal access token with the Copilot Requests permission. The server can't read it back to anyone, and
        a machine that gets it can use your subscription.
      </p>
      {!vault ? (
        <p className="bad">This server can't store tokens: its administrator sets AI_PROXY_VAULT_KEYS.</p>
      ) : (
        <form
          className="row wrap"
          onSubmit={(event) => {
            event.preventDefault();
            store.mutate();
          }}
        >
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={token}
            placeholder={account.credential.stored ? "Replace it with a new one" : "github_pat_…"}
            onChange={(event) => setToken(event.target.value)}
            aria-label="Fine-grained personal access token"
          />
          <button type="submit" disabled={!token.trim() || store.isPending}>
            {account.credential.stored ? "Replace" : "Store"}
          </button>
          {account.credential.stored && (
            <button type="button" className="danger" onClick={() => setRevoking(true)}>
              Revoke
            </button>
          )}
        </form>
      )}
      <ConfirmDialog
        open={revoking}
        title={`Revoke ${account.display_name}'s token?`}
        confirmLabel="Revoke"
        busy={revoke.isPending}
        onConfirm={() => {
          setRevoking(false);
          revoke.mutate();
        }}
        onCancel={() => setRevoking(false)}
      >
        The server forgets it and stops handing it to machines. Agents already running keep it until they exit; revoke it on GitHub too.
      </ConfirmDialog>
    </div>
  );
}

function AccountSettings({ account, windows }: { account: ProviderAccount; windows: UsageWindow[] }) {
  const notify = useToast();
  const client = useQueryClient();
  const [priority, setPriority] = useState(String(account.priority));
  const [users, setUsers] = useState(account.users.join(", "));
  const [limitValue, setLimitValue] = useState(account.limit ? String(account.limit.value) : "");
  const [limitUnit, setLimitUnit] = useState<DeclaredLimit["unit"]>(account.limit?.unit ?? "requests");
  const [limitWindow, setLimitWindow] = useState<UsageWindow>(account.limit?.window ?? windows[0] ?? "5h");
  const [removing, setRemoving] = useState(false);
  const done = () => {
    client.invalidateQueries({ queryKey: ["provider-accounts"] });
    client.invalidateQueries({ queryKey: ["provider-account", account.id] });
  };
  const save = useMutation({
    mutationFn: () =>
      api.updateProviderAccount(account.id, {
        priority: Number(priority) || 0,
        users: users.split(/[\s,]+/).filter(Boolean),
        limit: limitValue ? { window: limitWindow, unit: limitUnit, value: Number(limitValue) } : null,
      }),
    onSuccess: () => {
      notify("Saved.");
      done();
    },
    onError: (error: Error) => notify(error.message),
  });
  const toggle = useMutation({
    mutationFn: () => api.updateProviderAccount(account.id, { status: account.status === "active" ? "disabled" : "active" }),
    onSuccess: done,
    onError: (error: Error) => notify(error.message),
  });
  const clear = useMutation({ mutationFn: () => api.updateProviderAccount(account.id, { clear_observations: true }), onSuccess: done, onError: (error: Error) => notify(error.message) });
  const remove = useMutation({
    mutationFn: () => api.deleteProviderAccount(account.id),
    onSuccess: () => {
      notify(`Removed ${account.display_name}.`);
      done();
    },
    onError: (error: Error) => notify(error.message),
  });
  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <h3>Settings</h3>
      <div className="form-grid">
        <label className="field">
          Priority
          <input type="number" min={0} value={priority} onChange={(event) => setPriority(event.target.value)} />
          <small>Lower is tried first when the server chooses a subscription.</small>
        </label>
        <label className="field">
          Others who may start agents with it
          <input value={users} placeholder="user names, comma-separated" onChange={(event) => setUsers(event.target.value)} />
          <small>They never see or get its sign-in.</small>
        </label>
        <label className="field">
          Plan limit <span className="optional">optional</span>
          <input type="number" min={1} value={limitValue} placeholder="as your plan says" onChange={(event) => setLimitValue(event.target.value)} />
        </label>
        <label className="field">
          Counted in
          <span className="row">
            <select value={limitUnit} onChange={(event) => setLimitUnit(event.target.value as DeclaredLimit["unit"])} aria-label="Unit">
              <option value="requests">requests</option>
              <option value="tokens">tokens</option>
            </select>
            <select value={limitWindow} onChange={(event) => setLimitWindow(event.target.value as UsageWindow)} aria-label="Window">
              {(["5h", "24h", "7d", "month"] as UsageWindow[]).map((item) => (
                <option key={item} value={item}>
                  {WINDOW_LABELS[item].toLowerCase()}
                </option>
              ))}
            </select>
          </span>
        </label>
      </div>
      <div className="row wrap">
        <button type="submit" className="primary" disabled={save.isPending}>
          Save
        </button>
        <button type="button" onClick={() => toggle.mutate()}>
          {account.status === "active" ? "Turn off" : "Turn on"}
        </button>
        {(account.state === "rate_limited" || account.state === "quota_exhausted") && (
          <button type="button" onClick={() => clear.mutate()}>
            Forget the limits seen
          </button>
        )}
        <button type="button" className="danger" onClick={() => setRemoving(true)}>
          Remove
        </button>
      </div>
      <ConfirmDialog
        open={removing}
        title={`Remove ${account.display_name}?`}
        confirmLabel="Remove"
        busy={remove.isPending}
        onConfirm={() => {
          setRemoving(false);
          remove.mutate();
        }}
        onCancel={() => setRemoving(false)}
      >
        Its machines stop running it and its usage history goes. Your sign-ins on those machines stay as they are.
      </ConfirmDialog>
    </form>
  );
}

function Suspend({ account }: { account: ProviderAccount }) {
  const notify = useToast();
  const client = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => api.updateProviderAccount(account.id, { status: account.status === "active" ? "disabled" : "active" }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["provider-accounts"] }),
    onError: (error: Error) => notify(error.message),
  });
  return (
    <div className="row wrap">
      <button type="button" onClick={() => toggle.mutate()}>
        {account.status === "active" ? "Turn it off" : "Turn it back on"}
      </button>
      <span className="subtle">As an admin you can stop new agents using it; you can't use or change it.</span>
    </div>
  );
}

function Events({ account }: { account: ProviderAccount }) {
  const events = useQuery({ queryKey: ["provider-account-events", account.id], queryFn: () => api.providerAccountEvents(account.id) });
  const listed = events.data?.events ?? [];
  return (
    <details>
      <summary>History</summary>
      {listed.length === 0 ? (
        <p className="subtle">Nothing yet.</p>
      ) : (
        <table className="table">
          <tbody>
            {listed.map((event, index) => (
              <tr key={index}>
                <td className="subtle">{when(event.at)}</td>
                <td>{event.kind.replace(/_/g, " ")}</td>
                <td className="subtle">{[event.by, event.machine].filter(Boolean).join(" · ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  );
}
