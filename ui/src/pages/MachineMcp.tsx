import { Link } from "react-router-dom";
import type { FleetMachine } from "../api";
import { CodeLine } from "../components/CodeLine";
import { useClipboard } from "../hooks";

const ADD_STDIO = 'agent-machine mcp add github --env GITHUB_PERSONAL_ACCESS_TOKEN=... -- npx -y @modelcontextprotocol/server-github';
const ADD_URL = "agent-machine mcp add docs --url http://127.0.0.1:3000/mcp";

/**
 * The MCP servers a machine shares with the server's agents. They run on the machine (a command
 * started for each agent session, or a URL it proxies), and agents reach them through this server
 * and the machine's own connection, so the machine needs no open port.
 */
export function MachineMcp({ machine }: { machine: FleetMachine }) {
  const copy = useClipboard();
  const servers = machine.mcp?.servers ?? [];
  const usable = Boolean(machine.you.mcp);
  return (
    <section className="panel" aria-labelledby="mcp-title">
      <h2 id="mcp-title">MCP servers</h2>
      <p className="subtle">
        MCP servers {machine.name} shares with agents. Each runs on {machine.name} as the user agent-machine runs as. A command
        gets its own process for each agent session. A URL is reached from {machine.name}, so it can be on that machine's own network.
        Agents reach them through this server, and every call is recorded in the agent's session.
      </p>
      {servers.length === 0 ? (
        <p className="empty">{machine.name} doesn't share any MCP servers yet.</p>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Runs as</th>
              <th>About</th>
            </tr>
          </thead>
          <tbody>
            {servers.map((server) => (
              <tr key={server.name}>
                <td className="mono">{server.name}</td>
                <td>
                  {server.transport === "http" ? "A URL" : "A command"}
                  {!server.found && <span className="warn"> · not installed there</span>}
                </td>
                <td className="subtle">{server.description || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {servers.length > 0 && (
        <p>
          {!machine.connected
            ? `${machine.name} isn't connected, so agents can't use its MCP servers now.`
            : usable
              ? (
                <>
                  Give them to an agent on <Link to="/agents">Start an agent</Link>, or with <code>--mcp {machine.name}/{servers[0].name}</code> (
                  <code>--mcp {machine.name}</code> for all of them).
                </>
              )
              : `Only ${machine.owner ?? "its owner"} and the teams they let can give these to agents.`}
        </p>
      )}
      {machine.you.owner && (
        <>
          <h3>Share an MCP server</h3>
          <p className="subtle">
            On {machine.name}, add a command (the MCP server's stdio program) or a URL. Its command, environment and headers stay on
            the machine. The server only learns its name. It takes effect within a minute.
          </p>
          <CodeLine text={ADD_STDIO} onCopy={() => copy(ADD_STDIO, "Command copied")} />
          <CodeLine text={ADD_URL} onCopy={() => copy(ADD_URL, "Command copied")} />
          <p className="subtle">
            Remove one with <code>agent-machine mcp remove &lt;name&gt;</code>. Let organizations and teams use them on the{" "}
            {machine.you.manage ? <Link to={`/machines/${encodeURIComponent(machine.id)}/access`}>Access</Link> : "Access"} tab (Use MCP servers).
          </p>
        </>
      )}
    </section>
  );
}
