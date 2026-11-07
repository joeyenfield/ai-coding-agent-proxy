import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Marked, type Tokens } from "marked";
import { api, type FleetMachine, type MachineFileEntry } from "../api";
import { Icon } from "../components/Icon";
import { formatBytes } from "../format";
import { escapeHtml, mermaidBlock, slug, watchDiagrams } from "../markdown";

/** When an entry was last changed, from seconds since the epoch. */
const modifiedAt = (seconds?: number | null) => (seconds ? new Date(seconds * 1000).toLocaleString() : "");

/** Below this width the list sits above the file rather than beside it (styles.css, .file-viewer). */
const NARROW = "(max-width: 1100px)";
const narrow = () => typeof window !== "undefined" && window.matchMedia(NARROW).matches;

/** Files shown rendered, the way GitHub shows them, unless ?view=source. */
const isMarkdown = (path: string) => /\.(md|markdown|mdown|mkd)$/i.test(path);

/** Heading ids in a rendered file, prefixed (as GitHub does) so they can't clash with the console's own. */
const anchorId = (name: string) => `user-content-${name}`;

const dirOf = (path: string, separator: string) => path.slice(0, Math.max(path.lastIndexOf(separator), 0)) || path;

/**
 * Where a relative link in a Markdown file points on the machine: from the file's folder, or from the
 * launch folder for one starting with /, as on GitHub. The machine still refuses anything outside the
 * launch folders, so this only has to be right, not safe.
 */
export function resolveLink(file: string, root: string, href: string, separator: string): string {
  let decoded = href;
  try {
    decoded = decodeURIComponent(href);
  } catch {
    // Not percent-encoded after all: use it as written.
  }
  const base = decoded.startsWith("/") ? root.replace(/[\\/]+$/, "") : dirOf(file, separator);
  const parts = base.split(/[\\/]/);
  for (const part of decoded.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (parts.length > 1) parts.pop();
    } else parts.push(part);
  }
  return parts.join(separator);
}

/**
 * A Markdown file as HTML. Raw HTML in it is shown as text, links to other files open them here, and only
 * https pictures are loaded (the machine only sends text, so its own pictures are named instead).
 */
function renderMarkdown(text: string, file: string, root: string, separator: string): string {
  const markdown = new Marked({
    gfm: true,
    renderer: {
      html({ text }: Tokens.HTML | Tokens.Tag) {
        return escapeHtml(text);
      },
      code({ text, lang }: Tokens.Code) {
        return lang?.trim() === "mermaid" ? mermaidBlock(text) : false;
      },
      heading({ tokens, depth, text }: Tokens.Heading) {
        return `<h${depth} id="${escapeHtml(anchorId(slug(text)))}">${this.parser.parseInline(tokens)}</h${depth}>\n`;
      },
      link({ href, title, tokens }: Tokens.Link) {
        const inner = this.parser.parseInline(tokens);
        const titled = title ? ` title="${escapeHtml(title)}"` : "";
        if (/^(https?:|mailto:)/i.test(href)) return `<a href="${escapeHtml(href)}"${titled} target="_blank" rel="noreferrer">${inner}</a>`;
        if (href.startsWith("#")) return `<a href="${escapeHtml(href)}" data-anchor="${escapeHtml(href.slice(1))}"${titled}>${inner}</a>`;
        // Any other scheme (javascript:, data:, file:) isn't followed.
        if (/^[a-z][a-z0-9+.-]*:/i.test(href) || !href) return `<span class="doc-offsite">${inner}</span>`;
        const [target, fragment = ""] = href.split("#");
        const path = resolveLink(file, root, target, separator);
        const folder = target.endsWith("/");
        const query = new URLSearchParams(folder ? { path } : { path: dirOf(path, separator), file: path });
        return `<a href="?${escapeHtml(query.toString())}" data-${folder ? "folder" : "file"}="${escapeHtml(path)}" data-anchor="${escapeHtml(fragment)}"${titled}>${inner}</a>`;
      },
      image({ href, title, text }: Tokens.Image) {
        if (!/^https:/i.test(href)) return `<span class="file-md-picture" title="${escapeHtml(href)}">${escapeHtml(text || href)}</span>`;
        return `<img src="${escapeHtml(href)}" alt="${escapeHtml(text)}"${title ? ` title="${escapeHtml(title)}"` : ""} loading="lazy" referrerpolicy="no-referrer">`;
      },
    },
  });
  return markdown.parse(text) as string;
}

/**
 * The machine's file viewer: open the folders inside its launch folders and read their files. The
 * machine lists and reads them itself (agent-machine files on), hidden names included, and only text.
 * The open folder and file are in the address (?path=, ?file=, ?view=source), so a link opens the same view.
 */
export function MachineFiles({ machine }: { machine: FleetMachine }) {
  const [params, setParams] = useSearchParams();
  const current = params.get("path") ?? machine.roots[0] ?? "";
  const file = params.get("file") ?? "";
  const source = params.get("view") === "source";
  const separator = current.includes("\\") ? "\\" : "/";
  const root = machine.roots.find((item) => current === item || current.startsWith(item.replace(/[\\/]+$/, "") + separator)) ?? machine.roots[0] ?? "";
  const ready = machine.connected && Boolean(machine.files) && Boolean(machine.you.files);
  // On a phone the list sits above the file, so it folds away once a file is open.
  const [listOpen, setListOpen] = useState(() => !file || !narrow());
  const contentRef = useRef<HTMLElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const pendingAnchor = useRef("");

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
  const openFolder = (path: string) => {
    setListOpen(true);
    setParams({ path });
  };
  const showFile = (path: string, folder = current) => {
    setParams({ path: folder, file: path });
    if (narrow()) {
      setListOpen(false);
      requestAnimationFrame(() => contentRef.current?.scrollIntoView({ block: "start" }));
    }
  };
  const openFile = (entry: MachineFileEntry) => showFile(entry.path);
  const setView = (view: "source" | null) => {
    const next = new URLSearchParams(params);
    if (view) next.set("view", view);
    else next.delete("view");
    setParams(next, { replace: true });
  };

  const markdown = isMarkdown(file);
  const rendered = markdown && !source;
  const text = content.data && !content.data.binary ? content.data.content : null;
  const html = useMemo(() => (rendered && text != null ? renderMarkdown(text, file, root, separator) : ""), [rendered, text, file, root, separator]);

  // Draw the file's diagrams, then go to the heading a link named.
  useEffect(() => {
    const preview = previewRef.current;
    if (!html || !preview) return;
    const stop = watchDiagrams(preview);
    if (pendingAnchor.current) {
      document.getElementById(anchorId(pendingAnchor.current))?.scrollIntoView();
      pendingAnchor.current = "";
    }
    return stop;
  }, [html]);

  // Links in a rendered file open the files they name here, and its headings, without leaving the page.
  const follow = (event: MouseEvent<HTMLDivElement>) => {
    const anchor = (event.target as HTMLElement).closest("a");
    if (!anchor || anchor.target === "_blank" || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const { file: target, folder, anchor: heading = "" } = anchor.dataset;
    if (target === undefined && folder === undefined && !heading) return;
    event.preventDefault();
    if (folder !== undefined) openFolder(folder);
    else if (target !== undefined && target !== file) {
      pendingAnchor.current = heading;
      showFile(target, dirOf(target, separator));
    } else document.getElementById(anchorId(heading))?.scrollIntoView();
  };

  const missing = [
    !machine.connected && "it isn't connected",
    !machine.files && "files are off there (agent-machine files on)",
    machine.roots.length === 0 && "it has no launch folders (agent-machine roots add <dir>)",
  ].filter((reason): reason is string => Boolean(reason));

  if (!machine.you.files) {
    return <p className="empty">Only {machine.owner ?? "its owner"}, or the teams it lets (Access), can browse the files on {machine.name}.</p>;
  }
  if (missing.length) return <p className="empty">You can't browse its files now: {missing.join("; ")}.</p>;

  const collapsed = Boolean(file) && !listOpen;
  // A last newline ends the last line rather than starting another.
  const lines = text?.replace(/\n$/, "").split("\n") ?? [];
  const count = listing.data?.entries.length ?? 0;
  return (
    <div className={`file-viewer${collapsed ? " list-closed" : ""}`}>
      <section className="section file-browser" aria-labelledby="machine-files">
        <div className="section-head">
          <h2 id="machine-files">Files</h2>
          {file && (
            <button
              type="button"
              className="small file-list-toggle"
              aria-expanded={!collapsed}
              aria-controls="machine-file-list"
              onClick={() => setListOpen(collapsed)}
              title={collapsed ? "Show the files in this folder" : "Hide the files, to give the open file the room"}
            >
              <Icon name="list" size={14} />
              {collapsed ? `Show files${count ? ` (${count})` : ""}` : "Hide files"}
            </button>
          )}
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
            <button type="button" className="link-button mono" onClick={() => openFolder(root)} disabled={current === root && !collapsed}>
              {root}
            </button>
          )}
          {crumbs.map((crumb) => (
            <span key={crumb.path} className="folder-crumb">
              <span className="part-sep" aria-hidden="true">›</span>
              <button type="button" className="link-button mono" onClick={() => openFolder(crumb.path)} disabled={crumb.path === current && !collapsed}>
                {crumb.name}
              </button>
            </span>
          ))}
          {!collapsed && listing.data?.parent && (
            <button type="button" className="small folder-up" onClick={() => openFolder(listing.data.parent as string)} title="The folder above">
              <Icon name="up" size={14} />
              Up
            </button>
          )}
        </div>
        {collapsed ? null : listing.isPending ? (
          <p className="subtle folder-note">Asking {machine.name} for its files…</p>
        ) : listing.isError ? (
          <p className="bad folder-note">{machine.name} didn't list its files ({listing.error.message}).</p>
        ) : listing.data.entries.length === 0 ? (
          <p className="subtle folder-note">Nothing inside.</p>
        ) : (
          <ul className="file-list" id="machine-file-list" aria-label={`Files in ${current}`}>
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
        {!collapsed && listing.data?.truncated && <p className="subtle folder-note">Only the first {listing.data.entries.length} are shown.</p>}
      </section>

      <section className="section file-content" aria-labelledby="machine-file" ref={contentRef}>
        <div className="section-head">
          <h2 id="machine-file" className="mono">{file ? file.split(/[\\/]/).pop() : "No file open"}</h2>
          {content.data && (
            <span className="subtle">
              {formatBytes(content.data.size)}
              {content.data.modified ? ` · changed ${modifiedAt(content.data.modified)}` : ""}
            </span>
          )}
          {markdown && text != null && (
            <div className="category-bar file-view-switch" role="group" aria-label="Show the file as">
              <button type="button" aria-pressed={!source} onClick={() => setView(null)}>
                Preview
              </button>
              <button type="button" aria-pressed={source} onClick={() => setView("source")}>
                Source
              </button>
            </div>
          )}
        </div>
        {!file ? (
          <p className="empty">Choose a file to read it.</p>
        ) : content.isPending ? (
          <p className="subtle">Reading {file}…</p>
        ) : content.isError ? (
          <p className="bad">
            {machine.name} didn't read it ({content.error.message}).{" "}
            <button type="button" className="link-button" onClick={() => openFolder(file)}>
              Open it as a folder
            </button>
          </p>
        ) : content.data.binary ? (
          <p className="empty">It isn't text, so it isn't shown.</p>
        ) : (
          <>
            {content.data.truncated && <p className="subtle">It's longer than 1 MB: only the start is shown.</p>}
            {rendered ? (
              <div className="file-markdown" role="region" aria-label={`${file}, rendered`} onClick={follow}>
                <div className="markdown" ref={previewRef} dangerouslySetInnerHTML={{ __html: html }} />
              </div>
            ) : (
              <div className="file-text" role="region" aria-label={`The text of ${file}`} tabIndex={0}>
                <pre className="code file-lines" aria-hidden="true">
                  {lines.map((_, index) => `${index + 1}\n`).join("")}
                </pre>
                <pre className="code">{content.data.content}</pre>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
