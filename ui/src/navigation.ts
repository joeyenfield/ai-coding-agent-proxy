/**
 * The console's three parts: agents (1), the machines they and their model servers run on (2),
 * and the server, the hub every request passes through and is recorded by (3). Machines come
 * before the server because they're where the work happens: they sign in to the server and
 * supply every local model server; only hosted APIs and agents' own accounts aren't on a
 * machine. The rail, the breadcrumb above each page's title and the Overview all come from this list.
 *
 * Everything about a model server (its models, settings, logs, the containers that run it,
 * the machine's load) is on its machine's page (/machines/<id>), because the machine does
 * it. All machines opens the searchable fleet list; individual machines aren't listed in the rail.
 */
import type { IconName } from "./components/Icon";

export type PartId = "agents" | "server" | "machines";

export interface NavLinkItem {
  to: string;
  label: string;
  icon?: IconName;
  end?: boolean;
  /** Only site admins can use it (the server's own settings and stats); hidden from everyone else. */
  siteAdmin?: boolean;
  /** Only admins (of an organization, or site admins) can use it; hidden from members. */
  admin?: boolean;
}

/** A provider with several pages, shown as a label with its pages indented beneath. */
export interface NavGroupItem {
  label: string;
  icon?: IconName;
  items: NavLinkItem[];
}

export type NavItem = NavLinkItem | NavGroupItem;

export interface Part {
  id: PartId;
  number: number;
  label: string;
  /** One line on what the part is for. */
  summary: string;
  /** The part's landing page, linked from the breadcrumb above each of its pages. */
  home?: string;
  items: NavItem[];
}

export const PARTS: Part[] = [
  {
    id: "agents",
    number: 1,
    label: "Agents",
    summary: "Start a coding agent through the server, then watch it and type into it from any device.",
    home: "/agents",
    items: [
      // Several agents' terminals at once: the first page of the part.
      { to: "/live-agents", label: "Live agent view", icon: "grid" },
      { to: "/agents", label: "Start an agent", icon: "play" },
      { to: "/terminals", label: "Running agents", icon: "terminal" },
      { to: "/ask", label: "Ask a model", icon: "chat" },
      // Everywhere agents' calls go (machines' model servers, hosted APIs, subscriptions), with your usage and limits.
      { to: "/providers", label: "Providers", icon: "server" },
      // People's own AI plans (Claude, Codex, Copilot), apart from their sign-in to the server.
      { to: "/subscriptions", label: "Subscriptions", icon: "key" },
    ],
  },
  {
    id: "machines",
    number: 2,
    label: "Machines",
    summary: "Computers signed in to the server. Each runs model servers agents use (Ollama, Strata, Docker, containers), managed on its page.",
    home: "/machines",
    items: [
      { to: "/machines", label: "All machines", icon: "monitor" },
      // Signing in agent login or agent-machine login elsewhere, from here.
      { to: "/approve-device", label: "Approve a device", icon: "key" },
    ],
  },
  {
    id: "server",
    number: 3,
    label: "Server",
    summary: "The hub: every request agents make passes through it and is recorded, on its way to a machine.",
    items: [
      { to: "/live", label: "Live", icon: "activity" },
      { to: "/traffic", label: "Traffic", icon: "arrows" },
      { to: "/requests", label: "Requests", icon: "list" },
      { to: "/sessions", label: "Sessions", icon: "layers" },
      { to: "/server-stats", label: "Server stats", icon: "cpu", siteAdmin: true },
      // Every HTTP request and WebSocket, the refused ones too: why a machine shows offline.
      { to: "/access-log", label: "Access log", icon: "list", siteAdmin: true },
      // Every request saved for the request log's days, with payloads in debug mode.
      { to: "/api-log", label: "API log", icon: "book", siteAdmin: true },
      // Who changed what, chained by hash: admins read their organization's, site admins everyone's.
      { to: "/audit", label: "Audit log", icon: "shield", admin: true },
      { to: "/settings", label: "Server settings", icon: "settings", siteAdmin: true },
      { to: "/organization", label: "Organizations", icon: "users" },
      // Not /downloads: the server serves the files themselves there.
      { to: "/get", label: "Downloads", icon: "download" },
    ],
  },
];

export const isGroup = (item: NavItem): item is NavGroupItem => "items" in item;

/** Parts by id, whatever their order in the rail (the routing map draws them in the order a request flows). */
export function partsById(...ids: PartId[]): Part[] {
  return ids.map((id) => PARTS.find((part) => part.id === id) as Part);
}

/** The parts with only the pages this person can use: the server's own pages are for site admins, some for admins. */
export function visibleParts(siteAdmin: boolean, admin = siteAdmin): Part[] {
  if (siteAdmin) return PARTS;
  const allowed = (link: NavLinkItem) => !link.siteAdmin && (admin || !link.admin);
  const keep = (item: NavItem) => isGroup(item) || allowed(item);
  return PARTS.map((part) => ({
    ...part,
    items: part.items.filter(keep).map((item) => (isGroup(item) ? { ...item, items: item.items.filter(allowed) } : item)),
  }));
}

const matches = (pathname: string, to: string) => pathname === to || pathname.startsWith(`${to}/`);

/** The part a page belongs to, and the provider group within it (such as Ollama), or null for the Overview. */
export function partFor(pathname: string): { part: Part; group?: string } | null {
  for (const part of PARTS) {
    for (const item of part.items) {
      if (isGroup(item)) {
        if (item.items.some((link) => matches(pathname, link.to))) return { part, group: item.label };
      } else if (matches(pathname, item.to)) {
        return { part };
      }
    }
  }
  return null;
}
