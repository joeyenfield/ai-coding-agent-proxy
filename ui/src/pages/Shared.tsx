import { useState, type FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type PeerResources } from "../api";
import { useClipboard, useToast } from "../hooks";

type RemoteMachine = PeerResources["machines"][number];

/**
 * Shared with you: what organizations on linked servers let your organization use, for you (bridges, set up by
 * the admins on both sides). Each is named by its full name, which agent --backend and --mcp take too. A machine
 * shared for launches runs the agent there, as the other server's session; you follow it here.
 */
export function SharedPage() {
  const resources = useQuery({ queryKey: ["peer-resources"], queryFn: api.peerResources, refetchInterval: 15000 });
  const copy = useClipboard();
  const data = resources.data;
  return (
    <div className="page">
      <header className="page-head">
        <h1>Shared with you</h1>
        <p className="subtle">
          Providers, MCP servers, agent definitions and machines organizations on linked servers share with yours. Requests go
          through both servers, which both record them; their keys never leave their server.
        </p>
      </header>
      {resources.isError && <p className="bad">{resources.error.message}</p>}
      {!data && !resources.isError && <p className="subtle">Loading…</p>}
      {data && !data.providers.length && !data.mcp.length && !data.agents.length && !data.machines.length && (
        <p className="empty">Nothing is shared with you yet. Your organization's admins open bridges with other servers' organizations (Organizations → Sharing).</p>
      )}
      {data && data.machines.length > 0 && (
        <section className="section" aria-labelledby="shared-machines">
          <div className="section-head"><h2 id="shared-machines">Machines</h2></div>
          <ul className="link-list">
            {data.machines.map((machine) => <SharedMachine key={machine.id} machine={machine} resources={data} />)}
          </ul>
        </section>
      )}
      {data && data.providers.length > 0 && (
        <section className="section" aria-labelledby="shared-providers">
          <div className="section-head">
            <h2 id="shared-providers">Providers</h2>
            <p className="subtle">Start an agent with one: choose it as its backend, or <code>agent qwen . --backend &lt;full name&gt;</code>.</p>
          </div>
          <div className="table-scroll">
            <table className="table">
              <thead><tr><th>Full name</th><th>Type</th><th>Models</th><th /></tr></thead>
              <tbody>
                {data.providers.map((provider) => (
                  <tr key={provider.id}>
                    <td className="mono">{provider.id}</td>
                    <td>{provider.type}{provider.protocols.length ? ` (${provider.protocols.join(", ")})` : ""}</td>
                    <td>{provider.models.slice(0, 8).join(", ") || <span className="subtle">Any it serves</span>}</td>
                    <td><button type="button" className="link" onClick={() => void copy(provider.id, "Copied the full name")}>Copy</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {data && data.mcp.length > 0 && (
        <section className="section" aria-labelledby="shared-mcp">
          <div className="section-head"><h2 id="shared-mcp">MCP servers</h2></div>
          <ul>
            {data.mcp.map((server) => (
              <li key={server.id}><span className="mono">{server.id}</span>{server.description ? ` · ${server.description}` : ""}{server.connected ? "" : " · not connected"}</li>
            ))}
          </ul>
        </section>
      )}
      {data && data.agents.length > 0 && (
        <section className="section" aria-labelledby="shared-agents">
          <div className="section-head">
            <h2 id="shared-agents">Agent definitions</h2>
            <p className="subtle">They show in Start an agent under their full names, pinned to the version your organization let in.</p>
          </div>
          <ul>{data.agents.map((agent) => <li key={agent.id} className="mono">{agent.id}</li>)}</ul>
        </section>
      )}
    </div>
  );
}

function SharedMachine({ machine, resources }: { machine: RemoteMachine; resources: PeerResources }) {
  const notify = useToast();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [agent, setAgent] = useState(machine.agents[0] ?? "");
  const [project, setProject] = useState(machine.roots[0] ?? "");
  const [backend, setBackend] = useState("");
  const [model, setModel] = useState("");
  const server = machine.id.split(":", 2)[1]?.split("/", 1)[0];
  // What a launch there can use: what that server's organization shares with yours, by full name.
  const backends = resources.providers.filter((provider) => provider.id.startsWith(`provider:${server}/`));
  const folders = useQuery({ queryKey: ["peer-folders", machine.id], queryFn: () => api.peerFolders(machine.id), enabled: open });
  const launch = useMutation({
    mutationFn: () => api.peerLaunch({ machine: machine.id, agent, project, backend: backend || undefined, model: model || undefined }),
    onSuccess: (answer) => (notify(`Started ${agent} on ${machine.name}.`), navigate(`/terminals/${encodeURIComponent(answer.session_id)}`)),
    onError: (error: Error) => notify(error.message),
  });
  const canLaunch = machine.functions.includes("launch");
  return (
    <li className="link-card">
      <div className="row wrap">
        <strong>{machine.name}</strong>
        <span className="mono subtle">{machine.id}</span>
        <span className={`pill ${machine.connected ? "good" : "live"}`}>{machine.connected ? "Connected" : "Not connected"}</span>
        {canLaunch && <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}>Start an agent there</button>}
      </div>
      {open && (
        <form className="stack" onSubmit={(event: FormEvent) => (event.preventDefault(), launch.mutate())}>
          <p className="subtle">
            It runs on {machine.name} as {server}'s session, by its owner's sign-in there, and may use only what the bridge gives it. You watch and
            type into it here{machine.functions.includes("terminal") ? "" : " (watching only: typing isn't shared)"}.
          </p>
          <label className="field">Agent
            <select value={agent} onChange={(event) => setAgent(event.target.value)} required>
              {machine.agents.map((item) => <option key={item} value={item}>{item}</option>)}
              {resources.agents.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}
            </select>
          </label>
          <label className="field">Folder
            <select value={project} onChange={(event) => setProject(event.target.value)} required>
              {(folders.data?.folders ?? machine.roots.map((root) => ({ name: root, path: root }))).map((folder) => <option key={folder.path} value={folder.path}>{folder.path}</option>)}
            </select>
          </label>
          <label className="field">Provider
            <select value={backend} onChange={(event) => setBackend(event.target.value)}>
              <option value="">The one {server} picks</option>
              {backends.map((provider) => <option key={provider.id} value={provider.id}>{provider.id}</option>)}
            </select>
          </label>
          <label className="field">Model
            <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="As the provider names it" />
          </label>
          <div className="row wrap">
            <button type="submit" className="primary" disabled={!agent || !project || launch.isPending}>{launch.isPending ? "Starting…" : "Start"}</button>
          </div>
        </form>
      )}
    </li>
  );
}
