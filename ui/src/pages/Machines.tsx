import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, machineProviders, type FleetMachine, type RemovedMachine } from "../api";
import { CodeLine } from "../components/CodeLine";
import { Icon } from "../components/Icon";
import { PartTag } from "../components/PartTag";
import { StateDot } from "../components/ProviderState";
import { useAgents, useAuth, useClipboard, useMachines, useStatus, useToast } from "../hooks";
import { formatRelative } from "../format";
import { OS_LABELS, machinePath, providerName } from "./Machine";
import { OneLineInstall, serverAddress } from "./Downloads";

/** The fleet: machines signed in to this server. Each one's page has its model servers, containers, load and launches. */
export function MachinesPage() {
  const machines = useMachines();
  const status = useStatus();
  const { hash } = useLocation();
  const [search, setSearch] = useState("");
  const list = machines.data?.machines ?? [];
  const query = search.trim().toLowerCase();
  const shown = list.filter((machine) =>
    [machine.name, machine.id, machine.hostname, machine.address, machine.owner].some((value) => value?.toLowerCase().includes(query)),
  );
  const online = list.filter((machine) => machine.connected);
  const server = serverAddress(status.data?.proxy_url);

  useEffect(() => {
    if (hash === "#add" && machines.data) document.getElementById("add")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [hash, machines.data]);

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Machines</h1>
        <p className="lede">
          Computers signed in to this server. They run every local model server agents use (Ollama, Strata, Docker Model Runner
          and containers), and you manage each one, its models and its load from its machine's page. You can start agents on
          them too.
        </p>
      </header>

      {machines.isError && <p className="bad">{machines.error.message}</p>}
      {machines.data && (
        <dl className="stat-cards">
          <div>
            <dt>Connected</dt>
            <dd>
              {online.length} <small>of {list.length}</small>
            </dd>
          </div>
          <div>
            <dt>Model servers</dt>
            <dd>{list.reduce((count, machine) => count + machineProviders(machine, status.data?.backends).length, 0)}</dd>
          </div>
          <div>
            <dt>Taking launches</dt>
            <dd>{online.filter((machine) => machine.launches).length}</dd>
          </div>
          <div>
            <dt>Need an update</dt>
            <dd>{list.filter((machine) => machine.update?.available).length}</dd>
          </div>
        </dl>
      )}

      {machines.data && list.length === 0 && (
        <div id="add">
          <JoinSteps server={server} />
        </div>
      )}

      {machines.data && list.length > 0 && (
        <div className="filters">
          <input type="search" value={search} onChange={(event) => setSearch(event.target.value)}
            placeholder="Search by name, hostname, address or owner" aria-label="Search machines" />
          <span className="subtle" role="status">{shown.length} of {list.length} machines</span>
        </div>
      )}
      {machines.data && list.length > 0 && shown.length === 0 && <p className="subtle">No machines match your search.</p>}
      <div className="machine-list">
        {shown.map((machine) => (
          <MachineSummary key={machine.id} machine={machine} />
        ))}
      </div>

      {(machines.data?.removed ?? []).length > 0 && <RemovedMachines removed={machines.data?.removed ?? []} />}

      {list.length > 0 && (
        <details className="manual" id="add" open={hash === "#add" ? true : undefined}>
          <summary>Add another machine</summary>
          <JoinSteps server={server} />
        </details>
      )}
    </div>
  );
}

/** Machines taken out of the fleet: refused when they connect, until allowed back here. */
function RemovedMachines({ removed }: { removed: RemovedMachine[] }) {
  const client = useQueryClient();
  const notify = useToast();
  const restore = useMutation({
    mutationFn: (id: string) => api.restoreMachine(id),
    onSuccess: (_, id) => {
      notify(`${removed.find((item) => item.id === id)?.name ?? id} can join again; it reconnects within a minute.`);
      client.invalidateQueries({ queryKey: ["machines"] });
    },
    onError: (error: Error) => notify(error.message),
  });
  return (
    <section className="section" aria-labelledby="removed-machines">
      <div className="section-head">
        <h2 id="removed-machines">Removed</h2>
        <p className="subtle">Removed from this server, so they're refused when they connect. Allow one back and it joins again by itself.</p>
      </div>
      <ul className="provider-backends">
        {removed.map((item) => (
          <li key={item.id}>
            <StateDot tone="idle" />
            <span className="provider-backend-name">
              <strong>{item.name}</strong>
              <span className="subtle mono">{item.id}</span>
            </span>
            <span className="subtle">
              {item.owner && `${item.owner}'s · `}removed {formatRelative(new Date(item.at * 1000).toISOString())}
              {item.by && ` by ${item.by}`}
            </span>
            <span className="state-text">
              <button type="button" className="small" disabled={restore.isPending} onClick={() => restore.mutate(item.id)}>
                Allow back
              </button>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function MachineSummary({ machine }: { machine: FleetMachine }) {
  const status = useStatus();
  const agents = useAgents();
  const providers = machineProviders(machine, status.data?.backends);
  const installed = (agents.data ?? []).filter((agent) => machine.agents.includes(agent.id));
  return (
    <article className={`machine-card ${machine.connected ? "is-online" : ""}`} aria-labelledby={`machine-${machine.id}`}>
      <header className="machine-card-head">
        <span className="provider-icon" aria-hidden="true">
          <Icon name="monitor" size={20} />
        </span>
        <div className="machine-card-title">
          <h2 id={`machine-${machine.id}`}>
            <Link to={machinePath(machine.id)}>{machine.name}</Link>
            <span className={`pill ${machine.connected ? "good" : ""}`}>
              <StateDot tone={machine.connected ? "good" : "idle"} />{" "}
              {machine.connected ? "Online" : `Seen ${formatRelative(new Date(machine.last_seen * 1000).toISOString())}`}
            </span>
            {machine.update?.available && <span className="pill live">Update available</span>}
          </h2>
          <p className="subtle">
            {OS_LABELS[machine.os] ?? machine.os} {machine.arch}
            {machine.owner && <> · {machine.owner}</>}
            {machine.address && <> · <span className="mono">{machine.address}</span></>}
            {machine.version && <> · agent-machine {machine.version}</>}
          </p>
        </div>
        <div className="row wrap machine-card-actions">
          <Link className="button" to={machinePath(machine.id)}>
            Open
          </Link>
        </div>
      </header>
      <dl className="machine-facts">
        <div>
          <dt>Model servers</dt>
          <dd className="chips">
            {providers.length ? (
              providers.map((provider) => (
                <Link key={provider.id} className={`pill ${provider.backend ? "accent" : ""}`} to={machinePath(machine.id, provider.id)}>
                  {provider.backend ?? providerName(provider)}
                  {provider.container ? " (container)" : ""}
                </Link>
              ))
            ) : (
              <span className="subtle">None found</span>
            )}
          </dd>
        </div>
        <div>
          <dt>Agents</dt>
          <dd>
            {installed.length ? `${installed.length} installed` : <span className="subtle">None of the server's agents</span>}
            {machine.launches ? <span className="subtle"> · takes launches</span> : null}
          </dd>
        </div>
      </dl>
    </article>
  );
}

function JoinSteps({ server }: { server: string }) {
  const copy = useClipboard();
  const steps = [
    {
      text: "Ollama, Strata and Docker Model Runner running on it are found by themselves. To start, stop and tune them from here, and pull, load and delete their models:",
      command: "agent-machine servers on",
    },
    {
      text: "To let the server run Strata or Ollama there in containers, with Docker or Podman (whichever it has):",
      command: "agent-machine containers on",
    },
    {
      text: "To start agents on it from here, allow it and name the folders they may work in (this needs the agent command there too, signed in with agent login):",
      command: "agent-machine launches on",
    },
    { text: "", command: "agent-machine roots add ~/code" },
    { text: "Then start it: it joins the fleet and stays in it, in the background.", command: "agent-machine start" },
  ];
  return (
    <div className="panel join-steps">
      <div className="panel-head">
        <h2>Add a machine</h2>
        <p className="subtle">
          Every machine joins the same way, whether it runs models, agents or both. Containers and launches stay off until the
          machine allows them.
        </p>
      </div>
      <ol className="steps">
        <li>
          <p>
            On the machine, install agent-machine and sign it in with one command. It starts at boot and joins this server's fleet
            (for agent too, leave out <code>--only agent-machine</code>):
          </p>
          <OneLineInstall server={server} only="agent-machine" service />
          <p className="subtle">
            Or get its build, an installer or a package from <Link to="/get">Downloads</Link>, and sign it in yourself:
          </p>
          <CodeLine text={`agent-machine login ${server}`} onCopy={() => copy(`agent-machine login ${server}`, "Command copied")} />
        </li>
        {steps.map((step) => (
          <li key={step.command} className={step.text ? "" : "continued"}>
            {step.text && <p>{step.text}</p>}
            <CodeLine text={step.command} onCopy={() => copy(step.command, "Command copied")} />
          </li>
        ))}
      </ol>
      <SandboxSteps server={server} />
    </div>
  );
}

const RUNTIMES = [
  // Inside a container, 127.0.0.1 is the container itself; these names reach the computer running it.
  // Docker on Linux only knows its name with --add-host; Podman always knows its own.
  { id: "docker", label: "Docker", host: "host.docker.internal", addHost: true },
  { id: "podman", label: "Podman", host: "host.containers.internal", addHost: false },
] as const;
type Runtime = (typeof RUNTIMES)[number]["id"];
const RUNTIME_KEY = "ai-proxy-sandbox-runtime";

// Each shell's line continuation and home folder, for commands split over lines.
const SHELLS = [
  // $HOME rather than ~: the paths are quoted, and a quoted ~ isn't expanded.
  { id: "bash", label: "Linux / macOS", continuation: " \\", home: "$HOME/" },
  { id: "powershell", label: "PowerShell", continuation: " `", home: "${HOME}\\" },
] as const;
type Shell = (typeof SHELLS)[number]["id"];
const SHELL_KEY = "ai-proxy-sandbox-shell";

function Switch<T extends string>({ label, options, value, onChange }: { label: string; options: readonly { id: T; label: string }[]; value: T; onChange: (value: T) => void }) {
  return (
    <div className="env-switch" role="radiogroup" aria-label={label}>
      {options.map((item) => (
        <button key={item.id} type="button" role="radio" aria-checked={value === item.id} onClick={() => onChange(item.id)}>
          {item.label}
        </button>
      ))}
    </div>
  );
}

/** A choice kept in the browser, so the page comes back with it. */
function useRemembered<T extends string>(key: string, options: readonly { id: T }[], fallback: T): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => options.find((item) => item.id === localStorage.getItem(key))?.id ?? fallback);
  return [
    value,
    (next: T) => {
      setValue(next);
      localStorage.setItem(key, next);
    },
  ];
}

/** A machine of its own in a container (sandbox/Dockerfile): agents started on it can only touch its /workspace. */
function SandboxSteps({ server }: { server: string }) {
  const copy = useClipboard();
  const auth = useAuth();
  const [runtime, setRuntime] = useRemembered<Runtime>(RUNTIME_KEY, RUNTIMES, "docker");
  const [shell, setShell] = useRemembered<Shell>(SHELL_KEY, SHELLS, navigator.userAgent.includes("Windows") ? "powershell" : "bash");
  const engine = RUNTIMES.find((item) => item.id === runtime) ?? RUNTIMES[0];
  const style = SHELLS.find((item) => item.id === shell) ?? SHELLS[0];
  // Quoted placeholders: a bare <...> is a redirection in both shells.
  const user = auth.data?.required ? (auth.data.identity?.user ?? '"your-user"') : null;
  let address = server;
  let hostGateway = false;
  try {
    const url = new URL(server);
    if (["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) {
      url.hostname = engine.host;
      address = url.toString().replace(/\/$/, "");
      hostGateway = engine.addHost;
    }
  } catch {
    // Not a URL: use it as it is.
  }
  const build = `${runtime} build -f sandbox/Dockerfile -t agent-sandbox .`;
  const run = [
    `${runtime} run -d --name agent-sandbox --restart unless-stopped`,
    ...(hostGateway ? ["  --add-host=host.docker.internal:host-gateway"] : []),
    `  -e AI_PROXY_SERVER=${address}`,
    ...(user ? [`  -e AI_PROXY_USER=${user} -e AI_PROXY_PASSWORD="your password"`] : []),
    "  -v agent-sandbox-home:/home/agent -v agent-sandbox-workspace:/workspace agent-sandbox",
  ].join(`${style.continuation}\n`);
  return (
    <div className="sandbox-steps">
      <div className="install-command-head">
        <h3>Or run a sandbox machine in a container</h3>
        <div className="row wrap">
          <Switch label="Container engine" options={RUNTIMES} value={runtime} onChange={setRuntime} />
          <Switch label="Shell" options={SHELLS} value={shell} onChange={setShell} />
        </div>
      </div>
      <p className="subtle">
        A container that is a machine of its own, with Claude Code, Codex, OpenCode, Qwen Code and Copilot CLI installed. It signs in,
        joins the fleet with launches on for <code>/workspace</code> only, and agents you start on it can't touch the computer it runs
        on beyond that folder.
      </p>
      <ol className="steps">
        <li>
          <p>Build its image, from a copy of this repository:</p>
          <CodeLine text={build} onCopy={() => copy(build, "Command copied")} />
        </li>
        <li>
          <p>
            Run it{user ? ", signed in as you" : ""}. It shows up here as <strong>agent-sandbox</strong>
            {user ? "; its sign-in is kept in the agent-sandbox-home volume, so you can leave the password out of later runs" : ""}.
          </p>
          <CodeLine text={run} onCopy={() => copy(run, "Command copied")} />
          <p className="subtle">
            Use <code>-v "{style.home}projects:/workspace"</code> instead of the workspace volume to work on a folder of that computer's, and
            add <code>-v "{style.home}sandbox-ssh:/ssh:ro"</code> for git keys. The README's sandbox section lists every setting.
          </p>
          {runtime === "podman" && shell === "bash" && (
            <p className="subtle">
              Podman restarts it after a reboot only with its restart service on: <code>systemctl --user enable podman-restart</code>{" "}
              (with <code>loginctl enable-linger</code> to start it before you sign in).
            </p>
          )}
        </li>
      </ol>
    </div>
  );
}
