import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { API_ORIGIN, api, apiUrl, type DownloadFile, type DownloadKind } from "../api";
import { CodeLine } from "../components/CodeLine";
import { Icon } from "../components/Icon";
import { PartTag } from "../components/PartTag";
import { useClipboard, useStatus } from "../hooks";
import { formatBytes } from "../format";

const OS_LABELS: Record<string, string> = { windows: "Windows", linux: "Linux", darwin: "macOS", freebsd: "FreeBSD", android: "Android" };
const KIND_LABELS: Record<DownloadKind, string> = {
  archive: "Archive with installer",
  deb: "Debian, Ubuntu (.deb)",
  rpm: "Fedora, RHEL, openSUSE (.rpm)",
  apk: "Alpine (.apk)",
  arch: "Arch (.pkg.tar.gz)",
  msi: "Windows installer (.msi)",
  pkg: "macOS installer (.pkg)",
  binary: "Program",
  installer: "Install script",
  uninstaller: "Uninstall script",
  android: "Android app (.apk)",
  wheel: "Python wheel (.whl)",
};
const COMPONENTS = [
  { id: "agent", label: "Client", summary: "agent and agent-proxy, for computers that start coding agents. Sign it in with agent login." },
  { id: "agent-machine", label: "agent-machine", summary: "Keeps a computer in the fleet: its LLM providers, containers and launches. Sign it in with agent-machine login." },
] as const;
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

const onAndroid = /android/i.test(navigator.userAgent);

/** The builds this server offers: every platform's client and agent-machine, their packages, and the Android app. Public: standalone before sign-in. */
export function DownloadsPage({ standalone = false }: { standalone?: boolean }) {
  const index = useQuery({ queryKey: ["downloads"], queryFn: api.downloads, refetchInterval: 30_000 });
  const status = useStatus();
  const copy = useClipboard();
  const server = serverAddress(status.data?.proxy_url);
  const files = index.data?.files ?? [];
  const platforms = useMemo(() => orderPlatforms(files.filter((file) => file.platform !== "android" && file.platform !== "python").map((file) => file.platform)), [files]);
  const [picked, setPicked] = useState<string | null>(null);
  const platform = picked && platforms.includes(picked) ? picked : platforms[0];
  const android = files.filter((file) => file.component === "android");
  const python = files.filter((file) => file.component === "python");
  const own = files.filter((file) => file.platform === platform);
  // The browser's own platform comes first, so its two archives are the quick links.
  const mine = files.filter((file) => file.platform === platforms[0] && file.kind === "archive");

  return (
    <div className="page">
      <header className="page-head">
        {!standalone && <PartTag />}
        <h1>Downloads</h1>
        <p className="lede">
          The client and agent-machine built for this server, for every platform, and the Android app. Machines that already
          have them update from here with <code>agent update</code> and <code>agent-machine update</code>.
        </p>
      </header>

      {index.isError && <p className="bad">{index.error.message}</p>}
      {index.data && (
        <dl className="stat-cards">
          <div>
            <dt>Server version</dt>
            <dd className="mono download-version">{index.data.server_version}</dd>
          </div>
          <div>
            <dt>Protocol</dt>
            <dd>{index.data.protocol}</dd>
          </div>
          <div>
            <dt>Build offered</dt>
            <dd className="mono download-version">{index.data.version ?? "None"}</dd>
          </div>
          <div>
            <dt>Files</dt>
            <dd>{files.length}</dd>
          </div>
        </dl>
      )}
      {files.length > 0 && (
        <section className="panel" aria-labelledby="one-line-install">
          <div className="panel-head">
            <h2 id="one-line-install">Install with one command</h2>
            <p className="subtle">
              Run it on the computer: it picks the build for its system, checks it, installs agent and agent-machine, and signs
              them in to this server. Send it to anyone who should join.
            </p>
          </div>
          <OneLineInstall server={server} />
          <p className="subtle">
            Options after <code>sh -s --</code> (or after the PowerShell script block): <code>--only agent</code> or{" "}
            <code>--only agent-machine</code> for one of them, <code>--service</code> to start agent-machine at boot,{" "}
            <code>--no-login</code> to sign in later, <code>--prefix DIR</code> for another folder. In PowerShell they're{" "}
            <code>-Only</code>, <code>-Service</code>, <code>-NoLogin</code> and <code>-Prefix</code>.
          </p>
        </section>
      )}

      {(android.length > 0 || mine.length > 0) && (
        <section className="panel quick-downloads" aria-labelledby="quick-downloads">
          <h2 id="quick-downloads">Get them</h2>
          <div className="quick-download-list">
            {android.length > 0 && (
              // /android always answers with this server's app, so it's a link to keep.
              <a className={`quick-download ${onAndroid ? "is-first" : ""}`} href={apiUrl("/android")} download>
                <Icon name="download" size={18} />
                <span>
                  <strong>Android app</strong>
                  <small>
                    {formatBytes(android[0].size)} · {server.replace(/^https?:\/\//, "")}/android
                  </small>
                </span>
              </a>
            )}
            {mine.map((file) => (
              <a key={file.path} className="quick-download" href={apiUrl(file.url)} download>
                <Icon name="download" size={18} />
                <span>
                  <strong>
                    {file.component === "agent" ? "Client" : "agent-machine"} for {platformLabel(file.platform)}
                  </strong>
                  <small>
                    {file.name} · {formatBytes(file.size)}
                  </small>
                </span>
              </a>
            ))}
          </div>
        </section>
      )}

      {index.data?.version && !index.data.exact && (
        <p className="callout">
          This build is another commit of {index.data.version.split("+")[0]}, not exactly the server's version. It speaks the same
          protocol unless the code changed in between.
        </p>
      )}

      {index.data && files.length === 0 && (
        <div className="panel">
          <div className="panel-head">
            <h2>Nothing built yet</h2>
            <p className="subtle">
              The server offers the build of its own version from <code>dist/</code> on its computer. Build it there (it needs Go;
              the Android app needs the Android SDK and a JDK, and is skipped without them):
            </p>
          </div>
          <CodeLine text={index.data.hint || "./scripts/build-dist.sh"} onCopy={() => copy(index.data.hint || "./scripts/build-dist.sh", "Command copied")} />
        </div>
      )}

      {platform && (
        <section className="panel downloads-platform" aria-labelledby="downloads-platform">
          <div className="downloads-head">
            <h2 id="downloads-platform">{platformLabel(platform)}</h2>
            <label>
              <span className="subtle">Platform</span>
              <select value={platform} onChange={(event) => setPicked(event.target.value)}>
                {platforms.map((item) => (
                  <option key={item} value={item}>
                    {platformLabel(item)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {COMPONENTS.map((component) => (
            <ComponentDownloads
              key={component.id}
              label={component.label}
              summary={component.summary}
              component={component.id}
              files={own.filter((file) => file.component === component.id)}
              server={server}
              onCopy={copy}
            />
          ))}
          <details className="manual">
            <summary>The programs on their own</summary>
            <FileTable files={own.filter((file) => ["binary", "installer", "uninstaller"].includes(file.kind))} server={server} onCopy={copy} />
          </details>
        </section>
      )}

      {android.length > 0 && (
        <section className="panel" aria-labelledby="downloads-android">
          <div className="panel-head">
            <h2 id="downloads-android">Android app</h2>
            <p className="subtle">
              Your servers' consoles on your phone. Open this page on the phone and tap the file to install it (allow installing
              from your browser when Android asks). It says when it's older than a server it opens.
            </p>
          </div>
          <FileTable files={android} server={server} onCopy={copy} />
        </section>
      )}

      {python.length > 0 && (
        <section className="panel" aria-labelledby="downloads-python">
          <div className="panel-head">
            <h2 id="downloads-python">Python client and agent-machine</h2>
            <p className="subtle">
              The same programs written in Python, for any computer with Python 3.9 or later, including ones with no build above.
              They install as <code>agent</code> and <code>agent-machine</code>, like the programs above, so install one or the other. They do
              everything the others do, containers and model servers included.
            </p>
          </div>
          <FileTable files={python} server={server} onCopy={copy} />
        </section>
      )}

      {index.data && (
        <section className="panel" aria-labelledby="downloads-uninstall">
          <div className="panel-head">
            <h2 id="downloads-uninstall">Uninstall</h2>
            <p className="subtle">
              Removes agent, agent-proxy and agent-machine from the computer, with agent-machine's service and the PATH entry. Sign-ins
              and settings stay unless you add <code>--purge</code> (<code>-Purge</code> in PowerShell), which signs them out and
              deletes them too. <code>--only agent</code> or <code>--only agent-machine</code> removes one of them.
            </p>
          </div>
          <OneLineUninstall server={server} />
        </section>
      )}

      {files.length > 0 && (
        <p className="subtle">
          Every file is listed with its SHA-256; <code>sha256sum</code> (or <code>Get-FileHash</code> in PowerShell) shows a
          download's. Signed in already? Update in place with <code>agent update</code> or <code>agent-machine update</code>, and
          see which machines need it on <Link to="/machines">Machines</Link>.
        </p>
      )}
    </div>
  );
}

function ComponentDownloads({ label, summary, component, files, server, onCopy }: {
  label: string;
  summary: string;
  component: string;
  files: DownloadFile[];
  server: string;
  onCopy: (text: string, label?: string) => void;
}) {
  const archive = files.find((file) => file.kind === "archive");
  const packages = files.filter((file) => file.kind !== "archive" && file.kind !== "binary" && file.kind !== "installer");
  if (!archive && packages.length === 0) return null;
  const commands = archive ? installCommands(archive, server, component) : [];
  return (
    <div className="downloads-component">
      <h3>{label}</h3>
      <p className="subtle">{summary}</p>
      {archive && (
        <div className="row wrap">
          <a className="button primary" href={apiUrl(archive.url)} download>
            <Icon name="download" size={14} />
            {archive.name}
          </a>
          <span className="subtle">{formatBytes(archive.size)}</span>
        </div>
      )}
      {commands.length > 0 && (
        <>
          <p className="subtle">Or on the computer itself:</p>
          {commands.map((command) => (
            <CodeLine key={command} text={command} onCopy={() => onCopy(command, "Command copied")} />
          ))}
        </>
      )}
      {packages.length > 0 && (
        <>
          <p className="subtle">As a package:</p>
          <FileTable files={packages} server={server} onCopy={onCopy} />
        </>
      )}
    </div>
  );
}

function FileTable({ files, server, onCopy }: { files: DownloadFile[]; server: string; onCopy: (text: string, label?: string) => void }) {
  if (files.length === 0) return <p className="subtle">None in this build.</p>;
  return (
    <div className="table-scroll">
      <table className="table compact downloads-table">
        <thead>
          <tr>
            <th>File</th>
            <th>Kind</th>
            <th className="num">Size</th>
            <th>SHA-256</th>
          </tr>
        </thead>
        <tbody>
          {files.map((file) => (
            <tr key={file.path}>
              <td>
                <a href={apiUrl(file.url)} download className="mono">
                  {file.name}
                </a>
                {packageCommand(file, server) && <div className="subtle mono downloads-command">{packageCommand(file, server)}</div>}
              </td>
              <td className="nowrap">{KIND_LABELS[file.kind] ?? file.kind}</td>
              <td className="num nowrap">{formatBytes(file.size)}</td>
              <td>
                <button type="button" className="small mono downloads-sha" title={file.sha256} onClick={() => onCopy(file.sha256, "SHA-256 copied")}>
                  {file.sha256.slice(0, 12)}…
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The commands that fetch an archive from this server, unpack it and run its installer, then sign the program in. */
function installCommands(archive: DownloadFile, server: string, component: string): string[] {
  const url = server + archive.url;
  const login = component === "agent-machine" ? `agent-machine login ${server}` : `agent login ${server}`;
  if (archive.os === "windows") {
    const folder = component === "agent-machine" ? "agent-machine-setup" : "agent-setup";
    return [
      `$d = "$env:TEMP\\${folder}"; Invoke-WebRequest ${url} -OutFile "$d.zip"; Expand-Archive "$d.zip" $d -Force; powershell -ExecutionPolicy Bypass -File "$d\\${archive.platform}\\install.ps1"`,
      login,
    ];
  }
  return [`curl -fsSL ${url} | tar xz && ./${archive.platform}/install.sh`, login];
}

/** The package manager's command for a downloaded package, run where it was saved. */
function packageCommand(file: DownloadFile, server: string): string | null {
  const fetch = `curl -fsSLO ${server}${file.url} && `;
  switch (file.kind) {
    case "deb":
      return `${fetch}sudo apt install ./${file.name}`;
    case "rpm":
      return `${fetch}sudo dnf install ./${file.name}`;
    case "apk":
      return `${fetch}sudo apk add --allow-untrusted ./${file.name}`;
    case "arch":
      return `${fetch}sudo pacman -U ./${file.name}`;
    case "pkg":
      return `${fetch}sudo installer -pkg ${file.name} -target /`;
    case "msi":
      return `msiexec /i ${file.name}`;
    case "wheel":
      return `pip install ${server}${file.url}`;
    default:
      return null;
  }
}

type InstallOptions = { only?: "agent" | "agent-machine"; service?: boolean };

/** The one-line installer for one system: sh for Linux, macOS and FreeBSD, else PowerShell. */
export function installLine(server: string, system: "sh" | "powershell", { only, service }: InstallOptions = {}): string {
  if (system === "sh") {
    const options = [only && `--only ${only}`, service && "--service"].filter(Boolean).join(" ");
    return `curl -fsSL ${server}/install.sh | sh${options ? ` -s -- ${options}` : ""}`;
  }
  const options = [only && `-Only ${only}`, service && "-Service"].filter(Boolean).join(" ");
  return options ? `& ([scriptblock]::Create((irm ${server}/install.ps1))) ${options}` : `irm ${server}/install.ps1 | iex`;
}

/** The one-line installers this server writes for itself, the browser's own system first. */
export function OneLineInstall({ server, only, service }: { server: string } & InstallOptions) {
  return <OneLines sh={installLine(server, "sh", { only, service })} powershell={installLine(server, "powershell", { only, service })} />;
}

/** The uninstallers: the same file for every server, so they work whichever one installed the programs. */
function OneLineUninstall({ server }: { server: string }) {
  return <OneLines sh={`curl -fsSL ${server}/uninstall.sh | sh`} powershell={`irm ${server}/uninstall.ps1 | iex`} />;
}

function OneLines({ sh, powershell }: { sh: string; powershell: string }) {
  const copy = useClipboard();
  const lines = [
    { label: "Linux, macOS, FreeBSD", command: sh },
    { label: "Windows (PowerShell)", command: powershell },
  ];
  if (browserPlatform().startsWith("windows")) lines.reverse();
  return (
    <div className="one-line-install">
      {lines.map((line) => (
        <div key={line.label}>
          <span className="subtle">{line.label}</span>
          <CodeLine text={line.command} onCopy={() => copy(line.command, "Command copied")} />
        </div>
      ))}
    </div>
  );
}

/** Where other computers reach this server: its configured address, unless that's loopback and the browser isn't. */
export function serverAddress(proxyUrl: string | undefined): string {
  if (API_ORIGIN) return API_ORIGIN;
  const origin = window.location.origin;
  if (!proxyUrl) return origin;
  try {
    const host = new URL(proxyUrl).hostname;
    return LOOPBACK.has(host) && !LOOPBACK.has(window.location.hostname) ? origin : proxyUrl.replace(/\/$/, "");
  } catch {
    return origin;
  }
}

/** The browser's own platform first, then the rest as the build lists them. */
function orderPlatforms(found: string[]): string[] {
  const unique = [...new Set(found)];
  const guess = browserPlatform();
  const score = (platform: string) => (platform === guess ? 0 : platform.startsWith(guess.split("-")[0] + "-") ? 1 : 2);
  return unique.sort((a, b) => score(a) - score(b) || a.localeCompare(b));
}

export function browserPlatform(): string {
  const data = (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData;
  const agent = `${data?.platform ?? ""} ${navigator.userAgent}`.toLowerCase();
  const arm = /arm|aarch64/.test(agent);
  if (agent.includes("win")) return arm ? "windows-arm64" : "windows-amd64";
  // Browsers report Intel on Apple silicon too; most Macs now are arm64.
  if (agent.includes("mac")) return "darwin-arm64";
  if (agent.includes("freebsd")) return "freebsd-amd64";
  if (agent.includes("linux") && !agent.includes("android")) return arm ? "linux-arm64" : "linux-amd64";
  return "linux-amd64";
}

function platformLabel(platform: string): string {
  const [os, arch] = platform.split("-");
  return `${OS_LABELS[os] ?? os} ${arch ?? ""}`.trim();
}
