import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type FleetMachine, type MachineFileEntry } from "../api";
import { Icon } from "../components/Icon";
import { formatBytes } from "../format";

/** When an entry was last changed, from seconds since the epoch. */
const modifiedAt = (seconds?: number | null) => (seconds ? new Date(seconds * 1000).toLocaleString() : "");

/**
 * The machine's file viewer: open the folders inside its launch folders and read their files. The
 * machine lists and reads them itself (agent-machine files on), never anything hidden, and only text.
 * The open folder and file are in the address (?path=, ?file=), so a link opens the same view.
 */
export function MachineFiles({ machine }: { machine: FleetMachine }) {
  const [params, setParams] = useSearchParams();
  const current = params.get("path") ?? machine.roots[0] ?? "";
  const file = params.get("file") ?? "";
  const separator = current.includes("\\") ? "\\" : "/";
  const root = machine.roots.find((item) => current === item || current.startsWith(item.replace(/[\\/]+$/, "") + separator)) ?? machine.roots[0] ?? "";
  const ready = machine.connected && Boolean(machine.files) && Boolean(machine.you.files);

  const listing = useQuery({
    queryKey: ["machine-files", machine.id, current],
    queryFn: () => api.machineFiles(machine.id, current),
    enabled: ready && Boolean(current),
    staleTime: 10_000,
    retry: false,
  });
  const content = useQuery({
    queryKey: ["machine-file", machine.id, file],
    queryFn: () => api.machineFileContent(machine.id, file),
    enabled: ready && Boolean(file),
    staleTime: 10_000,
    retry: false,
  });

  // The crumbs from the launch folder down to the open one, each a way back to it.
  const crumbs = useMemo(() => {
    const below = current.slice(root.replace(/[\\/]+$/, "").length).split(/[\\/]+/).filter(Boolean);
    return below.map((name, index) => ({ name, path: root.replace(/[\\/]+$/, "") + separator + below.slice(0, index + 1).join(separator) }));
  }, [current, root, separator]);
  const openFolder = (path: string) => setParams({ path });
  const openFile = (entry: MachineFileEntry) => setParams({ path: current, file: entry.path });

  const missing = [
    !machine.connected && "it isn't connected",
    !machine.files && "files are off there (agent-machine files on)",
    machine.roots.length === 0 && "it has no launch folders (agent-machine roots add <dir>)",
  ].filter((reason): reason is string => Boolean(reason));

  if (!machine.you.files) {
    return <p className="empty">Only {machine.owner ?? "its owner"}, or the teams it lets (Access), can browse the files on {machine.name}.</p>;
  }
  if (missing.length) return <p className="empty">You can't browse its files now: {missing.join("; ")}.</p>;

  // A last newline ends the last line rather than starting another.
  const lines = content.data?.content?.replace(/\n$/, "").split("\n") ?? [];
  return (
    <div className="file-viewer">
      <section className="section file-browser" aria-labelledby="machine-files">
        <div className="section-head">
          <h2 id="machine-files">Files</h2>
        </div>
        <div className="folder-picker-head">
          {machine.roots.length > 1 ? (
            <select aria-label="Launch folder" value={root} onChange={(event) => openFolder(event.target.value)}>
              {machine.roots.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          ) : (
            <button type="button" className="link-button mono" onClick={() => openFolder(root)} disabled={current === root}>
              {root}
            </button>
          )}
          {crumbs.map((crumb) => (
            <span key={crumb.path} className="folder-crumb">
              <span className="part-sep" aria-hidden="true">›</span>
              <button type="button" className="link-button mono" onClick={() => openFolder(crumb.path)} disabled={crumb.path === current}>
                {crumb.name}
              </button>
            </span>
          ))}
          {listing.data?.parent && (
            <button type="button" className="small folder-up" onClick={() => openFolder(listing.data.parent as string)} title="The folder above">
              <Icon name="up" size={14} />
              Up
            </button>
          )}
        </div>
        {listing.isPending ? (
          <p className="subtle folder-note">Asking {machine.name} for its files…</p>
        ) : listing.isError ? (
          <p className="bad folder-note">{machine.name} didn't list its files ({listing.error.message}).</p>
        ) : listing.data.entries.length === 0 ? (
          <p className="subtle folder-note">Nothing inside.</p>
        ) : (
          <ul className="file-list" aria-label={`Files in ${current}`}>
            {listing.data.entries.map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  className={entry.path === file ? "active" : undefined}
                  onClick={() => (entry.kind === "folder" ? openFolder(entry.path) : openFile(entry))}
                  title={modifiedAt(entry.modified)}
                >
                  <Icon name={entry.kind === "folder" ? "folder" : "file"} size={16} />
                  <span className="file-name">{entry.name}</span>
                  {entry.kind === "file" && <span className="subtle file-size">{formatBytes(entry.size ?? 0)}</span>}
                </button>
              </li>
            ))}
          </ul>
        )}
        {listing.data?.truncated && <p className="subtle folder-note">Only the first {listing.data.entries.length} are shown.</p>}
        <p className="subtle folder-note">Hidden files and folders (names starting with a dot) aren't shown.</p>
      </section>

      <section className="section file-content" aria-labelledby="machine-file">
        <div className="section-head">
          <h2 id="machine-file" className="mono">{file ? file.split(/[\\/]/).pop() : "No file open"}</h2>
          {content.data && (
            <span className="subtle">
              {formatBytes(content.data.size)}
              {content.data.modified ? ` · changed ${modifiedAt(content.data.modified)}` : ""}
            </span>
          )}
        </div>
        {!file ? (
          <p className="empty">Choose a file to read it.</p>
        ) : content.isPending ? (
          <p className="subtle">Reading {file}…</p>
        ) : content.isError ? (
          <p className="bad">{machine.name} didn't read it ({content.error.message}).</p>
        ) : content.data.binary ? (
          <p className="empty">It isn't text, so it isn't shown.</p>
        ) : (
          <>
            {content.data.truncated && <p className="subtle">It's longer than 1 MB: only the start is shown.</p>}
            <div className="file-text" role="region" aria-label={`The text of ${file}`} tabIndex={0}>
              <pre className="code file-lines" aria-hidden="true">
                {lines.map((_, index) => `${index + 1}\n`).join("")}
              </pre>
              <pre className="code">{content.data.content}</pre>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
