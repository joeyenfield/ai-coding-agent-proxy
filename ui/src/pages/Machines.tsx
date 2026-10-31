import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, machineProviders, type FleetMachine, type RemovedMachine } from "../api";
import { CodeLine } from "../components/CodeLine";
import { PartTag } from "../components/PartTag";
import { StateDot } from "../components/ProviderState";
import { useClipboard, useMachines, useStatus, useToast, useVersion } from "../hooks";
import { formatDate, formatRelative } from "../format";
import { OS_LABELS, machinePath } from "./Machine";
import { browserPlatform, installLine, serverAddress } from "./Downloads";

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
      {shown.length > 0 && (
        <div className="table-scroll">
          <table className="table fleet-table">
            <thead>
              <tr><th>Name</th><th>Architecture</th><th>Last contact</th><th>Functions</th></tr>
            </thead>
            <tbody>
              {shown.map((machine) => <MachineRow key={machine.id} machine={machine} />)}
            </tbody>
          </table>
        </div>
      )}

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

function MachineRow({ machine }: { machine: FleetMachine }) {
  const status = useStatus();
  const lastContact = new Date(machine.last_seen * 1000).toISOString();
  const functions = [
    { name: "Agents", enabled: machine.launches },
    { name: "Terminals", enabled: Boolean(machine.commands) },
    { name: "Model servers", enabled: machineProviders(machine, status.data?.backends).length > 0 },
    { name: "Server management", enabled: Boolean(machine.servers.enabled) },
    { name: "Containers", enabled: Boolean(machine.containers.enabled) },
    { name: "Updates", enabled: Boolean(machine.updates) },
    { name: "Tunnel", enabled: Boolean(machine.tunnel) },
    { name: "MCP servers", enabled: (machine.mcp?.servers?.length ?? 0) > 0 },
  ];
  return (
    <tr>
      <td data-label="Name">
        <Link className="fleet-name" to={machinePath(machine.id)}>{machine.name}</Link>
        <span className="row fleet-state"><StateDot tone={machine.connected ? "good" : "idle"} />{machine.connected ? "Online" : "Offline"}</span>
        {machine.owner && <span className="subtle">{machine.owner}</span>}
        {machine.update?.available && <span className="pill live">Update available</span>}
      </td>
      <td data-label="Architecture">
        <span className="mono">{machine.arch || "Unknown"}</span>
        <span className="subtle fleet-system">{OS_LABELS[machine.os] ?? machine.os}</span>
      </td>
      <td data-label="Last contact">
        <time dateTime={lastContact} title={formatDate(lastContact)}>{formatRelative(lastContact)}</time>
      </td>
      <td data-label="Functions">
        <ul className="fleet-functions" aria-label={`${machine.name} functions`}>
          {functions.map(({ name, enabled }) => (
            <li key={name} className={`pill ${enabled ? "good" : ""}`}>
              <StateDot tone={enabled ? "good" : "idle"} />
              <span>{name}: {enabled ? "Enabled" : "Disabled"}</span>
            </li>
          ))}
        </ul>
      </td>
    </tr>
  );
}

const SYSTEMS = [
  { id: "sh", label: "Linux, macOS, FreeBSD" },
  { id: "powershell", label: "Windows" },
  { id: "python", label: "Python" },
  { id: "container", label: "Docker container" },
] as const;
type System = (typeof SYSTEMS)[number]["id"];
const SYSTEM_KEY = "ai-proxy-join-system";

const INSTALLS = [
  {
    id: "machine",
    label: "agent-machine",
    about: "agent-machine keeps the computer in this server's fleet. Through it the server uses the model servers there (Ollama, Strata, Docker Model Runner) and, when you allow it, runs containers and starts agents there. Pick this for a computer that supplies models.",
  },
  {
    id: "both",
    label: "agent-machine and agent",
    about: "agent-machine, plus agent: the command you start coding agents with (agent claude ., agent qwen .), and agent-proxy, which carries their traffic to this server. Pick this for a computer you also code on, or that should take agent launches from the console.",
  },
] as const;
type Install = (typeof INSTALLS)[number]["id"];
const INSTALL_KEY = "ai-proxy-join-install";
const CONTAINER_ABOUT = "Docker container: a machine of its own in a Docker or Podman container, with agent-machine, agent and the coding agents (Claude Code, Codex, OpenCode, Qwen Code, Copilot CLI) installed. Agents started on it can only touch its /workspace folder, not the computer it runs on.";
const PYTHON_ABOUT = "Python: the same programs written in Python, for any computer with Python 3.9 or later, including one with no build here for its system. They do everything the others do, under the same command names, so install one or the other on a computer, not both.";

// What the machine lets the server do there: all off until it's turned on, on the machine itself.
const ALLOW = [
  { text: "Start, stop and tune its Ollama, Strata and Docker Model Runner, and manage their models", commands: ["agent-machine servers on"] },
  { text: "Run Strata or Ollama in containers, with Docker or Podman", commands: ["agent-machine containers on"] },
  { text: "Start agents there, in the folders you name (needs agent too)", commands: ["agent-machine launches on", "agent-machine roots add ~/code"] },
];

/** Adding a machine: pick its system and what to install, and get the one command to run there. */
function JoinSteps({ server }: { server: string }) {
  const copy = useClipboard();
  const [system, setSystem] = useRemembered<System>(SYSTEM_KEY, SYSTEMS, browserPlatform().startsWith("windows") ? "powershell" : "sh");
  const [install, setInstall] = useRemembered<Install>(INSTALL_KEY, INSTALLS, "machine");
  const [service, setService] = useState(true);
  const python = system === "python";
  // The container image has everything in it, so there's nothing to choose.
  const container = system === "container";
  const windows = system === "powershell";
  const command = installLine(server, windows ? "powershell" : "sh", { only: install === "machine" ? "agent-machine" : undefined, service });
  const login = `agent-machine login ${server}`;
  const chosen = INSTALLS.find((item) => item.id === install) ?? INSTALLS[0];
  // What the command does, in order, so nothing it changes on the computer is a surprise.
  const does = [
    "Downloads this server's build for the computer's processor and checks it against its SHA-256.",
    windows
      ? "Installs it into %LOCALAPPDATA%\\Programs\\agent-proxy and adds that folder to your PATH."
      : "Installs it into /usr/local/bin when run as root (with sudo), else ~/.local/bin.",
    ...(service
      ? [windows
        ? "Adds a scheduled task, agent-machine, that starts it whenever you sign in to Windows."
        : "Sets up a service that starts it at boot: systemd on Linux (a user service without sudo), launchd on macOS, rc.d on FreeBSD."]
      : []),
    install === "both"
      ? "Signs agent and agent-machine in to this server: each shows a code for you to approve. It never asks for your password."
      : "Signs agent-machine in to this server: it shows a code for you to approve. It never asks for your password.",
    "Starts agent-machine, which joins the fleet and appears in the list above.",
  ];
  return (
    <div className="panel join-steps">
      <div className="panel-head">
        <h2>Add a machine</h2>
        <p className="subtle">Pick the computer's system and what it should run, then run the command there.</p>
      </div>
      <div className="join-choices">
        <div className="join-choice">
          <span className="subtle">System</span>
          <Switch label="System" options={SYSTEMS} value={system} onChange={setSystem} />
        </div>
        {!container && (
          <div className="join-choice">
            <span className="subtle">Install</span>
            <Switch label="What to install" options={INSTALLS} value={install} onChange={setInstall} />
          </div>
        )}
      </div>
      {python && <p className="join-about">{PYTHON_ABOUT}</p>}
      <p className="join-about">{container ? CONTAINER_ABOUT : chosen.about}</p>
      {container ? (
        <SandboxSteps server={server} />
      ) : python ? (
        <>
          <PythonSteps server={server} agent={install === "both"} />
          <RemoveSteps server={server} system={system} install={install} />
        </>
      ) : (
        <>
          <CodeLine text={command} onCopy={() => copy(command, "Command copied")} />
          <label className="check">
            <input type="checkbox" checked={service} onChange={(event) => setService(event.target.checked)} />
            {windows ? "Start agent-machine whenever you sign in to Windows" : "Start agent-machine at boot, as a service"}
          </label>
          <div className="join-does">
            <p>What the command does:</p>
            <ol>
              {does.map((step) => <li key={step}>{step}</li>)}
            </ol>
            <p className="subtle">
              Approve {install === "both" ? "the codes" : "the code"} on <Link to="/approve-device">Approve a device</Link>. The machine
              belongs to whoever approves it.
            </p>
          </div>
          <details className="manual">
            <summary>Let the server do more there</summary>
            <p className="subtle">Everything that runs programs is off until you turn it on, on the machine. It applies within a minute.</p>
            <ul className="join-allow">
              {ALLOW.map((item) => (
                <li key={item.text}>
                  <p>{item.text}:</p>
                  {item.commands.map((line) => <CodeLine key={line} text={line} onCopy={() => copy(line, "Command copied")} />)}
                </li>
              ))}
            </ul>
          </details>
          <details className="manual">
            <summary>Install it by hand</summary>
            <p className="subtle">
              Get its build, an installer or a package from <Link to="/get">Downloads</Link>, then sign it in and start it:
            </p>
            <CodeLine text={login} onCopy={() => copy(login, "Command copied")} />
            <CodeLine text="agent-machine start" onCopy={() => copy("agent-machine start", "Command copied")} />
          </details>
          <RemoveSteps server={server} system={system} install={install} />
        </>
      )}
    </div>
  );
}

/** The one-line uninstaller (server/agent_server/install/uninstall.sh and .ps1) with its options. */
function uninstallLine(server: string, system: "sh" | "powershell", { only, purge }: { only?: "agent-machine"; purge: boolean }): string {
  if (system === "sh") {
    const options = [only && `--only ${only}`, purge && "--purge"].filter(Boolean).join(" ");
    return `curl -fsSL ${server}/uninstall.sh | sh${options ? ` -s -- ${options}` : ""}`;
  }
  const options = [only && `-Only ${only}`, purge && "-Purge"].filter(Boolean).join(" ");
  return options ? `& ([scriptblock]::Create((irm ${server}/uninstall.ps1))) ${options}` : `irm ${server}/uninstall.ps1 | iex`;
}

type Step = { command: string; text: string };

/** Taking a machine out again, for the system and install chosen above: its uninstaller, then its entry in the fleet. */
function RemoveSteps({ server, system, install, runtime = "docker", image = "" }: {
  server: string;
  system: System;
  install: Install;
  runtime?: Runtime;
  image?: string;
}) {
  const copy = useClipboard();
  const [purge, setPurge] = useState(false);
  const both = install === "both";
  let steps: Step[];
  let notes: string[] = [];
  if (system === "sh" || system === "powershell") {
    const windows = system === "powershell";
    steps = [{
      command: uninstallLine(server, system, { only: both ? undefined : "agent-machine", purge }),
      text: windows
        ? "Run it in PowerShell on the computer."
        : "Run it on the computer. If you installed with sudo (into /usr/local/bin, or as a system service), put sudo before its sh: | sudo sh.",
    }];
    notes = [
      windows
        ? "Removes the agent-machine scheduled task and stops agent-machine."
        : "Removes agent-machine's service (systemd, launchd or rc.d) and stops agent-machine.",
      ...(both ? ["Stops agent-proxy: agents running through it lose their connection to the server."] : []),
      ...(purge ? [both ? "Signs agent and agent-machine out of every server, which revokes their tokens." : "Signs agent-machine out of every server, which revokes its tokens."] : []),
      windows ? "Deletes the programs, the PATH entry and the empty folder." : "Deletes the programs.",
      purge
        ? `Deletes ${both ? "their" : "its"} settings, sign-ins, logs and caches.`
        : `Leaves ${both ? "their" : "its"} sign-ins and settings, so installing again picks up where it left off.`,
    ];
  } else if (system === "python") {
    steps = [
      { command: "agent-machine stop", text: "Stops agent-machine, so it leaves the fleet." },
      ...(both ? [{ command: "agent proxy stop", text: "Stops agent-proxy: agents running through it lose their connection to the server." }] : []),
      ...(purge
        ? [
          { command: "agent-machine logout", text: "Signs agent-machine out, revoking its token. It signs out of one server at a time: run it again for each." },
          ...(both ? [{ command: "agent logout", text: "Signs agent out the same way." }] : []),
        ]
        : []),
      { command: "python3 -m pip uninstall ai-proxy-agent-py", text: "Removes the Python programs (py instead of python3 on Windows; pipx uninstall if you used pipx)." },
      ...(purge
        ? [{
          command: both ? "rm -rf ~/.config/agent-machine ~/.config/agent-proxy ~/.cache/agent-proxy" : "rm -rf ~/.config/agent-machine",
          text: both
            ? "Deletes their settings, sign-ins and logs. On Windows they're agent-machine and agent-proxy in %APPDATA%, and .cache\\agent-proxy in your home folder."
            : "Deletes its settings, sign-ins and logs. On Windows it's agent-machine in %APPDATA%.",
        }]
        : []),
    ];
  } else {
    steps = [
      ...(purge
        ? [
          { command: `${runtime} exec agent-sandbox agent-machine logout`, text: "Signs its agent-machine out first, which revokes its token." },
          { command: `${runtime} exec agent-sandbox agent logout`, text: "And its agent." },
        ]
        : []),
      { command: `${runtime} rm -f agent-sandbox`, text: purge ? "Stops and removes the container." : "Stops and removes the container. Its sign-ins and workspace stay in their volumes, for the next one." },
      ...(purge
        ? [
          { command: `${runtime} volume rm agent-sandbox-home agent-sandbox-workspace`, text: "Deletes its volumes: its sign-ins, and everything in /workspace, so copy out anything you want to keep first." },
          ...(image ? [{ command: `${runtime} image rm ${image}`, text: "Frees the image's disk space." }] : []),
        ]
        : []),
    ];
  }
  return (
    <details className="manual">
      <summary>Remove it again</summary>
      <label className="check">
        <input type="checkbox" checked={purge} onChange={(event) => setPurge(event.target.checked)} />
        {system === "container" ? "Also sign it out and delete its volumes" : "Also sign out and delete its settings and sign-ins"}
      </label>
      <ol className="steps">
        {steps.map((step) => (
          <li key={step.command}>
            <p>{step.text}</p>
            <CodeLine text={step.command} onCopy={() => copy(step.command, "Command copied")} />
          </li>
        ))}
      </ol>
      {notes.length > 0 && (
        <div className="join-does">
          <p>What the command does:</p>
          <ol>{notes.map((note) => <li key={note}>{note}</li>)}</ol>
        </div>
      )}
      <p className="subtle">
        {system !== "container" && "Containers agent-machine ran and models it pulled stay. "}The machine stays in this list, offline,
        until you remove it: open its page and use <strong>Remove from this server</strong>.
      </p>
    </details>
  );
}

/** The Python agent-machine and agent: the server's wheel through pip, then signed in and started like the Go one. */
function PythonSteps({ server, agent }: { server: string; agent: boolean }) {
  const copy = useClipboard();
  const index = useQuery({ queryKey: ["downloads"], queryFn: api.downloads });
  const wheel = index.data?.files.find((file) => file.kind === "wheel");
  if (index.isPending) return <p className="subtle">Looking for this server's Python build...</p>;
  if (!wheel) {
    return (
      <p className="callout">
        This server has no Python build to hand out yet. It's built with the rest (<code>scripts/build-dist</code> on the server's
        computer); see <Link to="/get">Downloads</Link>.
      </p>
    );
  }
  const steps = [
    {
      command: `python3 -m pip install ${server}${wheel.url}`,
      text: "Installs this server's Python build, with the libraries it needs (httpx, websockets, starlette, uvicorn; pywinpty on Windows). It puts agent, agent-proxy and agent-machine on your PATH. On Windows, type py instead of python3.",
    },
    {
      command: `agent-machine login ${server}`,
      text: "Signs agent-machine in to this server: it shows a code for you to approve. It never asks for your password.",
    },
    ...(agent
      ? [{
        command: `agent login ${server}`,
        text: "Signs agent in too, with a code of its own, so you can start coding agents from that computer (agent claude ., agent qwen .).",
      }]
      : []),
    {
      command: "agent-machine start",
      text: "Starts agent-machine in the background, so it joins the fleet and appears in the list above. Nothing starts it after a restart: run this from your own startup to keep it in the fleet.",
    },
  ];
  return (
    <>
      <ol className="steps">
        {steps.map((step) => (
          <li key={step.command}>
            <p>{step.text}</p>
            <CodeLine text={step.command} onCopy={() => copy(step.command, "Command copied")} />
          </li>
        ))}
      </ol>
      <p className="subtle">
        If pip says the environment is externally managed (Debian, Ubuntu), use <code>pipx install</code> with the same address, or
        a virtualenv. Approve the codes on <Link to="/approve-device">Approve a device</Link>; the machine belongs to whoever approves it.
      </p>
    </>
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

// Where .github/workflows/images.yml publishes the images.
const SANDBOX_IMAGE = "ghcr.io/joeyenfield/agent-sandbox";

/** The published image's tag for a server version: X.Y.Z on its release, sha-<commit> for a commit of master, else latest. */
export function imageTag(version: string | null | undefined): string {
  if (!version) return "latest";
  const [release, build] = version.split("+");
  if (!build) return /^\d+\.\d+\.\d+$/.test(release) ? release : "latest";
  // A dirty or unknown build was never published.
  return /^[0-9a-f]{7}$/.test(build) ? `sha-${build}` : "latest";
}

/** A machine of its own in a container (sandbox/Dockerfile): agents started on it can only touch its /workspace. */
function SandboxSteps({ server }: { server: string }) {
  const copy = useClipboard();
  const version = useVersion();
  const [runtime, setRuntime] = useRemembered<Runtime>(RUNTIME_KEY, RUNTIMES, "docker");
  // The shell the commands are typed in, on the computer that runs the container: it decides how lines continue.
  const [shell, setShell] = useRemembered<Shell>(SHELL_KEY, SHELLS, browserPlatform().startsWith("windows") ? "powershell" : "bash");
  const engine = RUNTIMES.find((item) => item.id === runtime) ?? RUNTIMES[0];
  const style = SHELLS.find((item) => item.id === shell) ?? SHELLS[0];
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
  // The image of the server's own version, so the container's agent and agent-machine speak to it.
  const image = `${SANDBOX_IMAGE}:${imageTag(version.data?.version)}`;
  const run = [
    `${runtime} run -d --name agent-sandbox --restart unless-stopped`,
    ...(hostGateway ? ["  --add-host=host.docker.internal:host-gateway"] : []),
    `  -e AI_PROXY_SERVER=${address}`,
    `  -v agent-sandbox-home:/home/agent -v agent-sandbox-workspace:/workspace ${image}`,
  ].join(`${style.continuation}\n`);
  return (
    <div className="sandbox-steps">
      <div className="join-choices">
        <div className="join-choice">
          <span className="subtle">Engine</span>
          <Switch label="Container engine" options={RUNTIMES} value={runtime} onChange={setRuntime} />
        </div>
        <div className="join-choice">
          <span className="subtle">Shell</span>
          <Switch label="Shell" options={SHELLS} value={shell} onChange={setShell} />
        </div>
      </div>
      <ol className="steps">
        <li>
          <p>
            Run it. {runtime === "podman" ? "Podman" : "Docker"} downloads the image of this server's version from the GitHub Container
            Registry (<code>{image}</code>) the first time. On its first start it prints two sign-in codes
            (<code>{runtime} logs agent-sandbox</code>): approve both on <Link to="/approve-device">Approve a device</Link>. It shows up
            here as <strong>agent-sandbox</strong>, and its sign-in is kept in the agent-sandbox-home volume.
          </p>
          <CodeLine text={run} onCopy={() => copy(run, "Command copied")} />
          <p className="subtle">
            Use <code>-v "{style.home}projects:/workspace"</code> instead of the workspace volume to work on a folder of that computer's, and
            add <code>-v "{style.home}sandbox-ssh:/ssh:ro"</code> for git keys. To run your own changes, build the image from a copy of
            this repository (<code>{runtime} build -f sandbox/Dockerfile -t agent-sandbox .</code>) and run <code>agent-sandbox</code>{" "}
            instead. <Link to="/help/machines#a-sandbox-machine-in-docker">A sandbox machine in Docker</Link> lists every setting.
          </p>
          {runtime === "podman" && shell === "bash" && (
            <p className="subtle">
              Podman restarts it after a reboot only with its restart service on: <code>systemctl --user enable podman-restart</code>{" "}
              (with <code>loginctl enable-linger</code> to start it before you sign in).
            </p>
          )}
        </li>
      </ol>
      <RemoveSteps server={server} system="container" install="both" runtime={runtime} image={image} />
    </div>
  );
}
