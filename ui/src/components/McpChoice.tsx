import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Agent } from "../api";

/** The launcher's arguments for the MCP servers chosen. */
export function mcpArgs(chosen: string[]): string[] {
  return chosen.length ? ["--mcp", chosen.join(",")] : [];
}

/**
 * MCP servers machines share that you may give the agent: each runs on its machine (a command it
 * starts for the session, or a URL it reaches), and the agent reaches it through this server.
 */
export function McpChoice({ agent, chosen, onChange }: { agent: Agent; chosen: string[]; onChange: (chosen: string[]) => void }) {
  const shared = useQuery({ queryKey: ["mcp"], queryFn: api.sharedMcp, retry: false });
  const servers = shared.data ?? [];
  if (!servers.length) return null;
  if (!agent.mcp) {
    return <p className="subtle">{agent.name} can't use the MCP servers your machines share.</p>;
  }
  const toggle = (id: string, on: boolean) => onChange(on ? [...chosen, id] : chosen.filter((item) => item !== id));
  return (
    <fieldset className="mcp-choice">
      <legend>MCP servers</legend>
      <p className="subtle">
        Shared by your <Link to="/machines">machines</Link>. Each runs on its machine, and every call is recorded in the session.
      </p>
      {servers.map((server) => (
        <label key={server.id} className="check">
          <input
            type="checkbox"
            checked={chosen.includes(server.id)}
            disabled={!server.connected && !chosen.includes(server.id)}
            onChange={(event) => toggle(server.id, event.target.checked)}
          />
          <span>
            <span className="mono">{server.name}</span> on {server.machine_name}
            {server.description && <span className="subtle"> · {server.description}</span>}
            {!server.connected && <span className="subtle"> · offline</span>}
            {server.connected && !server.found && <span className="warn"> · not installed there</span>}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

/** The MCP servers a session opened by hand got, with the address to give the agent for each. */
export function McpUrls({ servers }: { servers?: { id: string; name: string; url: string }[] }) {
  if (!servers?.length) return null;
  return (
    <>
      <p className="subtle">If the agent's settings file isn't written for you, add these MCP servers to it (Streamable HTTP):</p>
      <ul className="plain">
        {servers.map((server) => (
          <li key={server.id}>
            <span className="mono">{server.name}</span>: <span className="mono wrap">{server.url}</span>
          </li>
        ))}
      </ul>
    </>
  );
}
