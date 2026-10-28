import { useEffect, useMemo, useRef, type MouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { Marked, type Tokens } from "marked";
import { api } from "../api";

/** The documentation's pages (docs/*.md), in the order the index lists them. */
export const DOC_PAGES: { id: string; label: string }[] = [
  { id: "index", label: "Overview" },
  { id: "getting-started", label: "Getting started" },
  { id: "architecture", label: "Architecture" },
  { id: "agents", label: "Agents" },
  { id: "mcp", label: "MCP servers" },
  { id: "machines", label: "Machines" },
  { id: "providers", label: "Providers" },
  { id: "console", label: "The console" },
  { id: "accounts", label: "Accounts and sharing" },
  { id: "configuration", label: "Configuration" },
  { id: "api", label: "API" },
  { id: "deployment", label: "Deployment" },
  { id: "development", label: "Development" },
];

const escapeHtml = (text: string) => text.replace(/[&<>"']/g, (char) => `&#${char.charCodeAt(0)};`);

/** A heading's anchor, the way GitHub makes them, so links written for the repository work here too. */
export function slug(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/[`*_]/g, "")
    .replace(/[^\p{L}\p{N}\- ]/gu, "")
    .replace(/ /g, "-");
}

/** Where a link in a page goes in the console: another page under /help, an anchor, an outside site, or nowhere. */
export function docHref(href: string): string | null {
  if (/^(https?:|mailto:)/.test(href)) return href;
  if (href.startsWith("#")) return href;
  const [file, fragment] = href.split("#");
  const page = /^([a-z0-9-]+)\.md$/.exec(file);
  // Files outside docs/ (../AGENTS.md, source files) aren't on the server.
  if (!page) return null;
  return `/help/${page[1] === "index" ? "" : page[1]}${fragment ? `#${fragment}` : ""}`.replace("/#", "#");
}

/** Markdown from docs/ as HTML: raw HTML in a page is shown as text, links and pictures point at the console. */
const markdown = new Marked({
  gfm: true,
  renderer: {
    html({ text }: Tokens.HTML | Tokens.Tag) {
      return escapeHtml(text);
    },
    heading({ tokens, depth, text }: Tokens.Heading) {
      return `<h${depth} id="${escapeHtml(slug(text))}">${this.parser.parseInline(tokens)}</h${depth}>\n`;
    },
    link({ href, title, tokens }: Tokens.Link) {
      const inner = this.parser.parseInline(tokens);
      const target = docHref(href);
      if (target === null) return `<span class="doc-offsite">${inner}</span>`;
      const outside = /^https?:/.test(target);
      return `<a href="${escapeHtml(target)}"${title ? ` title="${escapeHtml(title)}"` : ""}${outside ? ' target="_blank" rel="noreferrer"' : ""}>${inner}</a>`;
    },
    image({ href, title, text }: Tokens.Image) {
      const source = /^https?:/.test(href) ? href : `/help-files/${href.replace(/^\.\//, "")}`;
      return `<img src="${escapeHtml(source)}" alt="${escapeHtml(text)}"${title ? ` title="${escapeHtml(title)}"` : ""} loading="lazy">`;
    },
  },
});

/**
 * The documentation (docs/ in the repository), served by this server: what every part of the system
 * does and how to use it. Public, like Downloads, so it can be read before signing in.
 */
export function DocsPage() {
  const { page = "index" } = useParams();
  const known = DOC_PAGES.some((item) => item.id === page);
  const location = useLocation();
  const navigate = useNavigate();
  const body = useRef<HTMLDivElement>(null);
  const text = useQuery({ queryKey: ["help", page], queryFn: () => api.helpPage(page), enabled: known, retry: false, staleTime: 60_000 });
  const html = useMemo(() => (text.data ? (markdown.parse(text.data) as string) : ""), [text.data]);

  // Go to the heading the address names once the page is in, or to the top of a new page.
  useEffect(() => {
    if (!html) return;
    const id = decodeURIComponent(location.hash.slice(1));
    const target = id ? document.getElementById(id) : null;
    if (target) target.scrollIntoView();
    else window.scrollTo(0, 0);
  }, [html, location.hash]);

  // Links between pages stay in the console instead of reloading it.
  const follow = (event: MouseEvent<HTMLDivElement>) => {
    const anchor = (event.target as HTMLElement).closest("a");
    const href = anchor?.getAttribute("href");
    if (!anchor || !href || anchor.target === "_blank" || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (href.startsWith("/help") || href.startsWith("#")) {
      event.preventDefault();
      navigate(href.startsWith("#") ? `${location.pathname}${href}` : href);
    }
  };

  return (
    <div className="page wide docs-page">
      <div className="docs-layout">
        <nav className="docs-nav" aria-label="Documentation">
          <p className="docs-nav-title">Documentation</p>
          {DOC_PAGES.map((item) => (
            <NavLink key={item.id} to={item.id === "index" ? "/help" : `/help/${item.id}`} end>
              {item.label}
            </NavLink>
          ))}
          <a href="/docs" target="_blank" rel="noreferrer" className="docs-nav-api">
            API reference (OpenAPI)
          </a>
        </nav>
        <article className="docs-body" ref={body} onClick={follow}>
          {!known && <p className="empty">There's no page called {page}. Start at the <NavLink to="/help">overview</NavLink>.</p>}
          {known && text.isPending && <p className="subtle">Loading…</p>}
          {known && text.isError && (
            <p className="bad">
              This server doesn't have its documentation ({text.error.message}). It reads it from the docs folder of its checkout (or
              AI_PROXY_DOCS_DIR).
            </p>
          )}
          {html && <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />}
        </article>
      </div>
    </div>
  );
}
