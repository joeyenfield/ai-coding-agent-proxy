import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, LINK_KINDS, type LinkCode, type LinkInfo, type LinkKind, type LinkPeer } from "../api";
import { useClipboard, useToast } from "../hooks";
import { CodeLine } from "./CodeLine";
import { ConfirmDialog } from "./ConfirmDialog";

const KIND_LABELS: Record<LinkKind, string> = {
  provider: "Providers",
  mcp: "MCP servers",
  agent: "Agent definitions",
  launch: "Starting agents on machines",
  terminal: "Typing into those agents' terminals",
};

const stateLabel = (link: LinkInfo) =>
  link.state === "active" ? (link.connected ? "Active, connected" : "Active, not connected") : link.state === "paused" ? "Paused" : link.we_presented ? "Waiting for the other side" : link.waiting ? "Waiting for the other server to dial in" : "The other side presented: enter its code";

/**
 * Server settings → Links (site admins): this server's mesh id and key fingerprint, links to other servers set
 * up with a code from each side, and each link's policy (what kinds its organizations may let out and in, and
 * which organizations may bridge). Nothing crosses a link until organizations open bridges over it.
 */
export function LinksSettings() {
  const client = useQueryClient();
  const notify = useToast();
  const links = useQuery({ queryKey: ["links"], queryFn: api.links, refetchInterval: 5000 });
  const [meshId, setMeshId] = useState("");
  const [code, setCode] = useState<LinkCode | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: ["links"] });
  const saveMesh = useMutation({ mutationFn: () => api.setMeshId(meshId.trim()), onSuccess: () => (refresh(), notify("Saved the mesh id.")), onError: (error: Error) => notify(error.message) });
  const makeCode = useMutation({ mutationFn: api.linkCode, onSuccess: setCode, onError: (error: Error) => notify(error.message) });
  const rotate = useMutation({ mutationFn: api.rotateLinkKey, onSuccess: () => (refresh(), notify("A new link key. The old one stays valid for a day, so linked servers follow.")), onError: (error: Error) => notify(error.message) });
  const copy = useClipboard();
  if (links.isError) return <p className="bad">{links.error.message}</p>;
  if (!links.data) return <p className="subtle">Loading links…</p>;
  const { mesh } = links.data;
  const pending = links.data.links.filter((link) => link.state === "pending" && link.they_presented && !link.we_presented);
  return (
    <div className="stack">
      <section className="section" aria-labelledby="mesh-heading">
        <div className="section-head">
          <h2 id="mesh-heading">This server</h2>
          <p className="subtle">
            Linked servers know this one by its mesh id, chosen once, and check its key's fingerprint when you link. Nothing
            crosses a link by itself: organizations on both sides open bridges over it, and choose what crosses.
          </p>
        </div>
        {mesh.id ? (
          <dl className="facts">
            <dt>Mesh id</dt><dd className="mono">{mesh.id}</dd>
            <dt>Address</dt><dd className="mono">{mesh.url || "not set: set the public address under Server and providers"}</dd>
            <dt>Fingerprint</dt><dd className="mono">{mesh.fingerprint}</dd>
          </dl>
        ) : (
          <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), saveMesh.mutate())}>
            <input value={meshId} onChange={(event) => setMeshId(event.target.value)} placeholder="Mesh id, such as acme-eu" aria-label="Mesh id" pattern="[a-z0-9][a-z0-9-]{0,39}" required autoCapitalize="none" />
            <button type="submit" className="primary" disabled={saveMesh.isPending}>Save</button>
          </form>
        )}
        {mesh.id && (
          <div className="row wrap">
            <button type="button" className="primary" onClick={() => makeCode.mutate()} disabled={makeCode.isPending}>New link code</button>
            <button type="button" onClick={() => rotate.mutate()} disabled={rotate.isPending}>Rotate the link key</button>
          </div>
        )}
        {code && (
          <div className="callout">
            <strong>Give this to the other server's site admin</strong>
            <span>Read them the fingerprint too, so they know it's this server. The code works once, for an hour.</span>
            <CodeLine text={code.code} onCopy={() => void copy(code.code, "Copied the link code")} />
            <span className="mono">Mesh id {code.mesh_id} · {code.url} · fingerprint {code.fingerprint}</span>
          </div>
        )}
      </section>
      {mesh.id && <JoinLink onJoined={refresh} />}
      {pending.map((link) => <ConfirmPresented key={link.id} link={link} onJoined={refresh} />)}
      <section className="section" aria-labelledby="links-heading">
        <div className="section-head">
          <h2 id="links-heading">Links</h2>
        </div>
        {links.data.links.length ? (
          <ul className="link-list">
            {links.data.links.map((link) => (
              <li key={link.id} className="link-card">
                <div className="row wrap">
                  <button type="button" className="link" onClick={() => setOpen(open === link.id ? null : link.id)} aria-expanded={open === link.id}>
                    <strong>{link.peer}</strong>
                  </button>
                  <span className="mono subtle">{link.url}</span>
                  <span className={`pill ${link.state === "active" && link.connected ? "good" : "live"}`}>{stateLabel(link)}</span>
                </div>
                {open === link.id && <LinkDetailPanel id={link.id} onChanged={refresh} />}
              </li>
            ))}
          </ul>
        ) : <p className="empty">No links yet. Make a code here, and enter the other server's.</p>}
      </section>
    </div>
  );
}

/** Joining a server not met yet: its address, then the fingerprint its admin reads out, then its code. */
function JoinLink({ onJoined }: { onJoined: () => void }) {
  const notify = useToast();
  const [url, setUrl] = useState("");
  const [peer, setPeer] = useState<LinkPeer | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [code, setCode] = useState("");
  const inspect = useMutation({ mutationFn: () => api.inspectLink(url.trim()), onSuccess: (found) => (setPeer(found), setConfirmed(false)), onError: (error: Error) => notify(error.message) });
  const join = useMutation({
    mutationFn: () => api.joinLink({ url: peer?.url, code: code.trim(), fingerprint: peer?.fingerprint }),
    onSuccess: (link) => {
      notify(link.state === "active" ? `Linked with ${link.peer}.` : `Presented to ${link.peer}. The link is active once its admin enters this server's code.`);
      setPeer(null);
      setUrl("");
      setCode("");
      onJoined();
    },
    onError: (error: Error) => notify(error.message),
  });
  return (
    <section className="section" aria-labelledby="join-heading">
      <div className="section-head">
        <h2 id="join-heading">Link with another server</h2>
        <p className="subtle">Enter its address, check the fingerprint with its site admin, then enter the code they gave you.</p>
      </div>
      <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), inspect.mutate())}>
        <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="Its address, such as https://mesh.example.com" aria-label="Its address" required />
        <button type="submit" disabled={inspect.isPending}>Look it up</button>
      </form>
      {peer && (
        <form className="stack" onSubmit={(event: FormEvent) => (event.preventDefault(), join.mutate())}>
          <p>
            <span className="mono">{peer.mesh_id}</span> at <span className="mono">{peer.url}</span>, fingerprint <strong className="mono">{peer.fingerprint}</strong>
          </p>
          <label className="check">
            <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            This is the fingerprint its site admin read out to me
          </label>
          <label className="field">Its code
            <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="msl_…" required autoCapitalize="none" />
          </label>
          <div className="row wrap">
            <button type="submit" className="primary" disabled={!confirmed || join.isPending}>Link</button>
          </div>
        </form>
      )}
    </section>
  );
}

/** A server that presented to this one: its admin entered this server's code; enter theirs to finish. */
function ConfirmPresented({ link, onJoined }: { link: LinkInfo; onJoined: () => void }) {
  const notify = useToast();
  const [confirmed, setConfirmed] = useState(false);
  const [code, setCode] = useState("");
  const join = useMutation({
    mutationFn: () => api.joinLink({ link: link.id, code: code.trim() }),
    onSuccess: () => (notify(`Linked with ${link.peer}.`), onJoined()),
    onError: (error: Error) => notify(error.message),
  });
  return (
    <section className="section callout" aria-label={`${link.peer} presented`}>
      <strong>{link.peer} presented this server's code</strong>
      <span>Fingerprint <span className="mono">{link.fingerprint}</span>. Check it with its site admin, then enter the code they gave you.</span>
      <form className="inline-form" onSubmit={(event: FormEvent) => (event.preventDefault(), join.mutate())}>
        <label className="check">
          <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          The fingerprint matches
        </label>
        <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="msl_…" aria-label="Its code" required autoCapitalize="none" />
        <button type="submit" className="primary" disabled={!confirmed || join.isPending}>Link</button>
      </form>
    </section>
  );
}

function LinkDetailPanel({ id, onChanged }: { id: string; onChanged: () => void }) {
  const client = useQueryClient();
  const notify = useToast();
  const detail = useQuery({ queryKey: ["link", id], queryFn: () => api.link(id), refetchInterval: 10000 });
  const [draft, setDraft] = useState<{ out: LinkKind[]; in: LinkKind[]; orgs: string } | null>(null);
  const [removing, setRemoving] = useState(false);
  const done = () => (onChanged(), client.invalidateQueries({ queryKey: ["link", id] }));
  const save = useMutation({
    mutationFn: () => api.setLinkPolicy(id, { out: draft?.out ?? [], in: draft?.in ?? [], orgs: (draft?.orgs ?? "").split(/[\s,]+/).filter(Boolean) }),
    onSuccess: () => (notify("Saved the link policy. It applies at once, and the other server hears of it."), setDraft(null), done()),
    onError: (error: Error) => notify(error.message),
  });
  const action = useMutation({ mutationFn: (what: "pause" | "resume") => api.linkAction(id, what), onSuccess: done, onError: (error: Error) => notify(error.message) });
  const remove = useMutation({ mutationFn: () => api.removeLink(id), onSuccess: () => (setRemoving(false), onChanged()), onError: (error: Error) => notify(error.message) });
  if (detail.isError) return <p className="bad">{detail.error.message}</p>;
  if (!detail.data) return <p className="subtle">Loading…</p>;
  const link = detail.data;
  const current = draft ?? { out: link.policy.out ?? [], in: link.policy.in ?? [], orgs: (link.policy.orgs ?? []).join(", ") };
  const toggle = (side: "out" | "in", kind: LinkKind, on: boolean) =>
    setDraft({ ...current, [side]: on ? [...new Set([...current[side], kind])] : current[side].filter((item) => item !== kind) });
  return (
    <div className="stack link-detail">
      <p className="subtle">
        Fingerprint <span className="mono">{link.fingerprint}</span> · {link.dials ? "this server dials its link socket" : "it dials this server"}
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr><th>What</th><th>This server lets out</th><th>This server lets in</th><th>{link.peer} lets out</th><th>{link.peer} lets in</th></tr>
          </thead>
          <tbody>
            {LINK_KINDS.map((kind) => (
              <tr key={kind}>
                <td>{KIND_LABELS[kind]}</td>
                <td><input type="checkbox" aria-label={`Let ${KIND_LABELS[kind].toLowerCase()} out`} checked={current.out.includes(kind)} onChange={(event) => toggle("out", kind, event.target.checked)} /></td>
                <td><input type="checkbox" aria-label={`Let ${KIND_LABELS[kind].toLowerCase()} in`} checked={current.in.includes(kind)} onChange={(event) => toggle("in", kind, event.target.checked)} /></td>
                <td>{link.peer_policy.out?.includes(kind) ? "Yes" : "No"}</td>
                <td>{link.peer_policy.in?.includes(kind) ? "Yes" : "No"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <label className="field">Organizations here that may bridge over it (ids, or * for every one)
        <input value={current.orgs} onChange={(event) => setDraft({ ...current, orgs: event.target.value })} placeholder="*" />
      </label>
      <div className="row wrap">
        <button type="button" className="primary" disabled={!draft || save.isPending} onClick={() => save.mutate()}>Save the link policy</button>
        {link.state === "paused"
          ? <button type="button" onClick={() => action.mutate("resume")} disabled={action.isPending}>Resume</button>
          : <button type="button" onClick={() => action.mutate("pause")} disabled={action.isPending || link.state !== "active"}>Pause</button>}
        <button type="button" className="danger" onClick={() => setRemoving(true)}>Remove the link</button>
      </div>
      {link.bridges.length > 0 && (
        <p className="subtle">Bridges over it: {link.bridges.map((bridge) => `${bridge.org} ↔ ${bridge.peer_org} (${bridge.state})`).join(", ")}</p>
      )}
      <ConfirmDialog open={removing} title={`Remove the link to ${link.peer}?`} confirmLabel="Remove" busy={remove.isPending} error={remove.error?.message ?? null}
        onConfirm={() => remove.mutate()} onCancel={() => setRemoving(false)}>
        <p>Every bridge over it ends, and agents started over them stop. What each server recorded stays, under its own retention. Linking again needs new codes from both sides.</p>
      </ConfirmDialog>
    </div>
  );
}
