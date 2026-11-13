import { useEffect, useMemo, useRef, type MouseEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { Marked, type Tokens } from "marked";
import { api, apiUrl } from "../api";
import { escapeHtml, mermaidBlock, slug, watchDiagrams } from "../markdown";

export { slug };

/** The documentation's pages (docs/*.md), in sections, in the order the index lists them. */
export const DOC_SECTIONS: { title: string; pages: { id: string; label: string }[] }[] = [
  {
    title: "Start here",
    pages: [
      { id: "index", label: "Overview" },
      { id: "how-it-works", label: "How it works" },
      { id: "local", label: "On your own computer" },
      { id: "quickstart", label: "Quickstart: Docker or Podman" },
      { id: "quickstart-kubernetes", label: "Quickstart: Kubernetes" },
      { id: "getting-started", label: "Getting started" },
    ],
  },
  {
    title: "Use it",
    pages: [
      { id: "running-agents", label: "Agents" },
      { id: "console", label: "The console" },
      { id: "machines", label: "Machines" },
      { id: "providers", label: "Providers" },
      { id: "gpu", label: "GPUs in containers" },
      { id: "mcp", label: "MCP servers" },
      { id: "subscriptions", label: "Subscriptions" },
    ],
  },
  {
    title: "People and data",
    pages: [
      { id: "accounts", label: "Accounts and sharing" },
      { id: "peering", label: "Peering" },
      { id: "analytics", label: "Analytics and costs" },
      { id: "usage-limits", label: "Usage limits" },
      { id: "retention", label: "Retention" },
      { id: "audit", label: "Audit log" },
    ],
  },
  {
    title: "Run a server",
    pages: [
      { id: "deployment", label: "Deployment" },
      { id: "scaling", label: "Several server processes" },
      { id: "configuration", label: "Configuration" },
      { id: "security", label: "Security" },
      { id: "oidc", label: "OIDC authentication" },
      { id: "api-credentials", label: "Using API credentials" },
    ],
  },
  {
    title: "Technical overview",
    pages: [
      { id: "architecture", label: "Architecture" },
      { id: "flows", label: "Flows" },
      { id: "networking", label: "Networking and protocols" },
      { id: "peering-internals", label: "Peering internals" },
      { id: "scaling-internals", label: "Scaling internals" },
      { id: "local-internals", label: "Local instance internals" },
      { id: "api", label: "API" },
    ],
  },
  {
    title: "Contribute",
    pages: [
      { id: "development", label: "Development" },
      { id: "releases", label: "Releases and snapshots" },
      { id: "website", label: "Marketing website" },
    ],
  },
];

/** Every page, in order. */
export const DOC_PAGES = DOC_SECTIONS.flatMap((section) => section.pages);

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
    // A Mermaid diagram keeps its source in the page until mermaid.js draws it (renderDiagrams).
    code({ text, lang }: Tokens.Code) {
      if (lang?.trim() !== "mermaid") return false;
      return mermaidBlock(text);
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
      const source = /^https?:/.test(href) ? href : apiUrl(`/help-files/${href.replace(/^\.\//, "")}`);
      return `<img src="${escapeHtml(source)}" alt="${escapeHtml(text)}"${title ? ` title="${escapeHtml(title)}"` : ""} loading="lazy">`;
    },
  },
});

/**
 * The documentation (docs/ in the repository), served by this server: what every part of the system
 * does and how to use it. Public, like Downloads, so it can be read before signing in.
 */
export function DocsPage() {
  const { page: asked = "index" } = useParams();
  // The page was docs/agents.md: links and bookmarks to /help/agents still open it.
  const page = asked === "agents" ? "running-agents" : asked;
  const known = DOC_PAGES.some((item) => item.id === page);
  const location = useLocation();
  const navigate = useNavigate();
  const body = useRef<HTMLDivElement>(null);
  const text = useQuery({ queryKey: ["help", page], queryFn: () => api.helpPage(page), enabled: known, retry: false, staleTime: 60_000 });
  const html = useMemo(() => (text.data ? (markdown.parse(text.data) as string) : ""), [text.data]);

  // Draw the diagrams once the page is in, and again when the theme changes.
  useEffect(() => {
    const root = body.current;
    if (!html || !root) return;
    return watchDiagrams(root);
  }, [html]);

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
          {DOC_SECTIONS.map((section) => (
            <div key={section.title} className="docs-nav-section">
              <p className="docs-nav-title">{section.title}</p>
              {section.pages.map((item) => (
                <NavLink key={item.id} to={item.id === "index" ? "/help" : `/help/${item.id}`} end>
                  {item.label}
                </NavLink>
              ))}
            </div>
          ))}
          <a href={apiUrl("/docs")} target="_blank" rel="noreferrer" className="docs-nav-api">
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
