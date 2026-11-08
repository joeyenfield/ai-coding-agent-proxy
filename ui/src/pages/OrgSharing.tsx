import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type BridgeCode, type BridgeDetail, type BridgeInfo, type BridgeItem, type Org, type UsageWindow } from "../api";
import { CodeLine } from "../components/CodeLine";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useClipboard, useToast } from "../hooks";
import { parseTokens } from "./OrgUsage";

const WINDOWS: { id: UsageWindow; label: string }[] = [
  { id: "5h", label: "5 hours" }, { id: "24h", label: "24 hours" }, { id: "7d", label: "7 days" }, { id: "30d", label: "30 days" }, { id: "month", label: "Month" },
];

const keyOf = (item: BridgeItem) => [item.kind, item.machine ?? "", item.name ?? "", item.scope ?? ""].join("\u0000");

/** An item as people read it: what it is and where. */
export function itemLabel(item: BridgeItem): string {
  const home = item.scope === "home" ? " (for our own people's launches there)" : "";
  switch (item.kind) {
    case "provider":
      return `Provider ${item.name}${home}`;
    case "agent":
      return `Agent definition ${item.name}`;
    case "mcp":
      return `MCP server ${item.machine}/${item.name}${home}`;
    case "launch":
      return `Start agents on ${item.machine}`;
    case "terminal":
      return `Type into agents' terminals on ${item.machine}`;
  }
}

/**
 * Organization → Sharing (its admins): bridges with organizations on linked servers. Each admin enters the other's
 * code; then each side says what it lets out and what it lets in, and only what both say crosses. Nothing a provider
 * of yours does for the other side is used until you set this bridge a limit on it.
 */
export function OrgSharingTab({ org }: { org: Org }) {
  const client = useQueryClient();
  const bridges = useQuery({ queryKey: ["org-bridges", org.id], queryFn: () => api.orgBridges(org.id), refetchInterval: 10000 });
  const [open, setOpen] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["org-bridges", org.id] });
  if (bridges.isError) return <p className="bad">{bridges.error.message}</p>;
  if (!bridges.data) return <p className="subtle">Loading sharing…</p>;
  const data = bridges.data;
  if (!data.links.length && !data.bridges.length) {
    return (
      <p className="empty">
        No linked server lets {org.name} bridge yet. A site admin links this server with another (Server settings → Links), and says
        which organizations may bridge over the link.
      </p>
    );
  }
  return (
    <div className="stack">
      <p className="subtle">
        A bridge connects this organization, {org.name}, with an organization on another server linked to this one. Each side
        chooses which of its providers, MCP servers, agent definitions and machines the other may use. Both servers carry and
        record every request in full, and keys never leave their own server.
      </p>
      <NewBridge org={org} links={data.links} onJoined={refresh} />
      {data.bridges.length ? (
        <ul className="link-list">
          {data.bridges.map((bridge) => (
            <li key={bridge.id} className="link-card">
              <div className="row wrap">
                <button type="button" className="link" onClick={() => setOpen(open === bridge.id ? null : bridge.id)} aria-expanded={open === bridge.id}>
                  <strong>{bridge.peer_org}</strong>
                </button>
                <span className="subtle">at {bridge.server}</span>
                <span className={`pill ${bridge.state === "active" ? "good" : "live"}`}>{stateOf(bridge)}</span>
                <span className="subtle">{bridge.crossing.out.length} out · {bridge.crossing.in.length} in</span>
              </div>
              {open === bridge.id && <BridgePanel org={org} id={bridge.id} onChanged={refresh} />}
            </li>
          ))}
        </ul>
      ) : <p className="empty">No bridges yet.</p>}
    </div>
  );
}

const stateOf = (bridge: BridgeInfo) =>
  bridge.state === "active" ? "Open" : bridge.state === "paused" ? "Paused" : bridge.we_confirmed ? "Waiting for them to enter your code" : "They entered your code: enter theirs";

function NewBridge({ org, links, onJoined }: { org: Org; links: { id: string; server: string; connected: boolean }[]; onJoined: () => void }) {
  const notify = useToast();
  const copy = useClipboard();
  const [link, setLink] = useState(links[0]?.id ?? "");
  const [made, setMade] = useState<BridgeCode | null>(null);
  const [code, setCode] = useState("");
  const [found, setFound] = useState<{ org: string; name: string; server: string } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const make = useMutation({ mutationFn: () => api.bridgeCode(org.id, link), onSuccess: setMade, onError: (error: Error) => notify(error.message) });
  const inspect = useMutation({ mutationFn: () => api.inspectBridge(org.id, link, code.trim()), onSuccess: (answer) => (setFound(answer), setConfirmed(false)), onError: (error: Error) => notify(error.message) });
  const join = useMutation({
    mutationFn: () => api.joinBridge(org.id, { link, code: code.trim(), peer_org: found?.org ?? "" }),
    onSuccess: (bridge) => {
      notify(bridge.state === "active" ? `Bridged with ${bridge.peer_org}.` : `Entered ${bridge.peer_org}'s code. The bridge opens once they enter yours.`);
      setFound(null);
      setCode("");
      onJoined();
    },
    onError: (error: Error) => notify(error.message),
  });
  if (!links.length) return null;
  return (
    <section className="section" aria-labelledby="new-bridge">
      <div className="section-head">
        <h3 id="new-bridge">Open a bridge</h3>
        <p className="subtle">Make a code for the other organization's admin, and enter theirs. Check the organization each code names.</p>
      </div>
      <label className="field">Linked server
        <select value={link} onChange={(event) => setLink(event.target.value)}>
          {links.map((item) => <option key={item.id} value={item.id}>{item.server}{item.connected ? "" : " (not connected)"}</option>)}
        </select>
      </label>
      <div className="row wrap">
        <button type="button" onClick={() => make.mutate()} disabled={!link || make.isPending}>New bridge code</button>
      </div>
      {made && (
        <div className="callout">
          <strong>Give this to the other organization's admin</strong>
          <span>It names {org.name} (<span className="mono">{made.org}</span> at <span className="mono">{made.server}</span>) and works once.</span>
          <CodeLine text={made.code} onCopy={() => void copy(made.code, "Copied the bridge code")} />
        </div>
      )}
      <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), inspect.mutate())}>
        <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="Their code, msb_…" aria-label="Their bridge code" required autoCapitalize="none" />
        <button type="submit" disabled={!link || inspect.isPending}>Check it</button>
      </form>
      {found && (
        <form className="stack" onSubmit={(event: FormEvent) => (event.preventDefault(), join.mutate())}>
          <p>That code is <strong>{found.name}</strong> (<span className="mono">{found.org}</span>) at <span className="mono">{found.server}</span>.</p>
          <label className="check">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            That's the organization I expected
          </label>
          <p className="subtle">Both servers record everything that crosses, payloads included, each under its own retention.</p>
          <div className="row wrap">
            <button type="submit" className="primary" disabled={!confirmed || join.isPending}>Open the bridge</button>
          </div>
        </form>
      )}
    </section>
  );
}

function BridgePanel({ org, id, onChanged }: { org: Org; id: string; onChanged: () => void }) {
  const client = useQueryClient();
  const notify = useToast();
  const detail = useQuery({ queryKey: ["bridge", org.id, id], queryFn: () => api.bridge(org.id, id), refetchInterval: 10000 });
  const [ending, setEnding] = useState(false);
  const done = () => (onChanged(), client.invalidateQueries({ queryKey: ["bridge", org.id, id] }));
  const action = useMutation({ mutationFn: (what: "pause" | "resume" | "refresh") => api.bridgeAction(org.id, id, what), onSuccess: done, onError: (error: Error) => notify(error.message) });
  const end = useMutation({ mutationFn: () => api.endBridge(org.id, id), onSuccess: () => (setEnding(false), onChanged()), onError: (error: Error) => notify(error.message) });
  if (detail.isError) return <p className="bad">{detail.error.message}</p>;
  if (!detail.data) return <p className="subtle">Loading…</p>;
  const bridge = detail.data;
  return (
    <div className="stack link-detail">
      <div className="columns">
        <div>
          <h4>Crosses to {bridge.peer_org}</h4>
          {bridge.crossing.out.length ? <ul>{bridge.crossing.out.map((item) => <li key={keyOf(item)}>{itemLabel(item)}</li>)}</ul> : <p className="subtle">Nothing yet.</p>}
        </div>
        <div>
          <h4>Crosses to {org.name}</h4>
          {bridge.crossing.in.length ? <ul>{bridge.crossing.in.map((item) => <li key={keyOf(item)}>{itemLabel(item)}{item.teams?.length ? ` · ${item.teams.join(", ")}` : ""}</li>)}</ul> : <p className="subtle">Nothing yet.</p>}
        </div>
      </div>
      {bridge.state !== "pending" && <PolicyEditor org={org} bridge={bridge} onSaved={done} />}
      {bridge.crossing.out.some((item) => item.kind === "provider" && !item.scope) && <BridgeLimits org={org} bridge={bridge} onSaved={done} />}
      <div className="row wrap">
        <button type="button" onClick={() => action.mutate("refresh")} disabled={action.isPending}>Fetch what they share now</button>
        {bridge.state === "paused"
          ? <button type="button" onClick={() => action.mutate("resume")} disabled={action.isPending}>Resume</button>
          : <button type="button" onClick={() => action.mutate("pause")} disabled={action.isPending || bridge.state !== "active"}>Pause</button>}
        <button type="button" className="danger" onClick={() => setEnding(true)}>End the bridge</button>
      </div>
      <ConfirmDialog open={ending} title={`End the bridge with ${bridge.peer_org}?`} confirmLabel="End it" busy={end.isPending} error={end.error?.message ?? null}
        onConfirm={() => end.mutate()} onCancel={() => setEnding(false)}>
        <p>It ends on both sides, and agents its people started on your machines stop. What each server recorded stays, under its own retention.</p>
      </ConfirmDialog>
    </div>
  );
}

/** This side's policy: what it lets out (what it has, and machines whose owner granted the bridge) and what it lets in. */
function PolicyEditor({ org, bridge, onSaved }: { org: Org; bridge: BridgeDetail; onSaved: () => void }) {
  const notify = useToast();
  const [out, setOut] = useState<BridgeItem[] | null>(null);
  const [into, setInto] = useState<BridgeItem[] | null>(null);
  const currentOut = out ?? bridge.policy.out;
  const currentIn = into ?? bridge.policy.in;
  const offered = useMemo(() => {
    const list: BridgeItem[] = [];
    for (const provider of bridge.shareable.providers) {
      list.push({ kind: "provider", name: provider.name }, { kind: "provider", name: provider.name, scope: "home" });
    }
    for (const agent of bridge.shareable.agents) list.push({ kind: "agent", name: agent.name });
    for (const machine of bridge.shareable.machines) {
      if (machine.granted.includes("launch")) list.push({ kind: "launch", machine: machine.name });
      if (machine.granted.includes("terminal")) list.push({ kind: "terminal", machine: machine.name });
      if (machine.granted.includes("mcp")) for (const name of machine.mcp) list.push({ kind: "mcp", machine: machine.name, name });
    }
    return list;
  }, [bridge.shareable]);
  // What they let out is what we can let in; what we already let in stays listed even once they stop.
  const takeable = useMemo(() => {
    const seen = new Map<string, BridgeItem>();
    for (const item of [...bridge.peer_policy.out, ...bridge.policy.in]) seen.set(keyOf(item), { ...item, teams: undefined, digest: item.digest });
    return [...seen.values()];
  }, [bridge.peer_policy.out, bridge.policy.in]);
  const has = (list: BridgeItem[], item: BridgeItem) => list.find((entry) => keyOf(entry) === keyOf(item));
  const toggle = (list: BridgeItem[], set: (value: BridgeItem[]) => void, item: BridgeItem, on: boolean) =>
    set(on ? [...list.filter((entry) => keyOf(entry) !== keyOf(item)), item] : list.filter((entry) => keyOf(entry) !== keyOf(item)));
  const save = useMutation({
    mutationFn: () => api.setBridgePolicy(org.id, bridge.id, {
      out: currentOut.map(({ kind, name, machine, scope }) => ({ kind, name, machine, scope })),
      in: currentIn,
    }),
    onSuccess: () => (notify("Saved. Narrowing applies at once; something new crosses once their side says so too."), setOut(null), setInto(null), onSaved()),
    onError: (error: Error) => notify(error.message),
  });
  const ungranted = bridge.shareable.machines.filter((machine) => !machine.granted.length);
  return (
    <section className="section" aria-label="This side's policy">
      <div className="columns">
        <fieldset>
          <legend>{org.name} lets out</legend>
          {offered.length ? offered.map((item) => (
            <label key={keyOf(item)} className="check">
              <input type="checkbox" checked={Boolean(has(currentOut, item))} onChange={(event) => toggle(currentOut, setOut, item, event.target.checked)} />
              {itemLabel(item)}
            </label>
          )) : <p className="subtle">Nothing to let out: no providers, agent definitions of your own, or machines that let this bridge in.</p>}
          {ungranted.length > 0 && <p className="subtle">To share a machine, its owner lets this bridge use it on the machine's Access tab: {ungranted.map((machine) => machine.name).join(", ")}.</p>}
        </fieldset>
        <fieldset>
          <legend>{org.name} lets in</legend>
          {takeable.length ? takeable.map((item) => {
            const chosen = has(currentIn, item);
            return (
              <div key={keyOf(item)} className="stack tight">
                <label className="check">
                  <input type="checkbox" checked={Boolean(chosen)} onChange={(event) => toggle(currentIn, setInto, { ...item, teams: chosen?.teams ?? [] }, event.target.checked)} />
                  {itemLabel(item)}
                </label>
                {chosen && org.teams.length > 0 && (
                  <select multiple aria-label={`Teams that may use ${itemLabel(item)}`} value={chosen.teams ?? []}
                    onChange={(event) => toggle(currentIn, setInto, { ...chosen, teams: [...event.target.selectedOptions].map((option) => option.value) }, true)}>
                    {org.teams.map((team) => <option key={team.key} value={team.key}>{team.name}</option>)}
                  </select>
                )}
              </div>
            );
          }) : <p className="subtle">{bridge.peer_org} lets nothing out to {org.name} yet.</p>}
        </fieldset>
      </div>
      <div className="row wrap">
        <button type="button" className="primary" disabled={(!out && !into) || save.isPending} onClick={() => save.mutate()}>Save this side's policy</button>
      </div>
    </section>
  );
}

function BridgeLimits({ org, bridge, onSaved }: { org: Org; bridge: BridgeDetail; onSaved: () => void }) {
  const notify = useToast();
  const providers = bridge.crossing.out.filter((item) => item.kind === "provider" && !item.scope).map((item) => item.name ?? "");
  const [provider, setProvider] = useState(providers[0] ?? "");
  const [window, setWindow] = useState<UsageWindow>("month");
  const [tokens, setTokens] = useState("");
  const parsed = parseTokens(tokens);
  const save = useMutation({
    mutationFn: () => api.setBridgeLimit(org.id, bridge.id, { provider, window, tokens: parsed ?? -1 }),
    onSuccess: () => (notify("Saved the limit."), setTokens(""), onSaved()),
    onError: (error: Error) => notify(error.message),
  });
  const remove = useMutation({
    mutationFn: (limit: { provider: string; window: UsageWindow }) => api.removeBridgeLimit(org.id, bridge.id, limit.provider, limit.window),
    onSuccess: onSaved, onError: (error: Error) => notify(error.message),
  });
  return (
    <section className="section" aria-label="Limits">
      <h4>Limits</h4>
      <p className="subtle">What {bridge.peer_org}'s people may use of each of your providers over this bridge, together. Nothing is used until there's a limit.</p>
      {bridge.limits.length ? (
        <ul>
          {bridge.limits.map((limit) => (
            <li key={`${limit.provider} ${limit.window}`}>
              {limit.provider.replace(/^backend:/, "")}: {limit.tokens.toLocaleString()} tokens a {WINDOWS.find((item) => item.id === limit.window)?.label ?? limit.window}
              <button type="button" className="link" onClick={() => remove.mutate({ provider: limit.provider.replace(/^backend:/, ""), window: limit.window as UsageWindow })}>Remove</button>
            </li>
          ))}
        </ul>
      ) : <p className="warn">No limit yet: {bridge.peer_org} can't use your providers.</p>}
      <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), save.mutate())}>
        <select value={provider} onChange={(event) => setProvider(event.target.value)} aria-label="Provider">
          {providers.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
        <select value={window} onChange={(event) => setWindow(event.target.value as UsageWindow)} aria-label="Window">
          {WINDOWS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
        <input value={tokens} onChange={(event) => setTokens(event.target.value)} placeholder="Tokens, such as 1m" aria-label="Tokens" required />
        <button type="submit" disabled={parsed === null || save.isPending}>Set the limit</button>
      </form>
    </section>
  );
}
