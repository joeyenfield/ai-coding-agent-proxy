/**
 * The console's three parts, in the order a request flows: an agent is started (1), its
 * traffic passes through the server, the hub that records it (2), and a machine's LLM
 * provider answers it (3). Machines sign in to the server and supply every local model
 * server; only hosted APIs and agents' own accounts aren't on a machine. The rail, the
 * breadcrumb above each page's title and the Overview all come from this list.
 *
 * Everything about a model server (its models, settings, logs, the containers that run it,
 * the machine's load) is on its machine's page (/machines/<id>), because the machine does
 * it. The rail lists the machines themselves under Machines (App.tsx).
 */
import type { IconName } from "./components/Icon";

export type PartId = "agents" | "server" | "machines";

export interface NavLinkItem {
  to: string;
  label: string;
  icon?: IconName;
  end?: boolean;
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
      { to: "/agents", label: "Start an agent", icon: "play" },
      { to: "/terminals", label: "Running agents", icon: "terminal" },
    ],
  },
  {
    id: "server",
    number: 2,
    label: "Server",
    summary: "The hub: every request agents make passes through it and is recorded, on its way to a machine.",
    items: [
      { to: "/live", label: "Live", icon: "activity" },
      { to: "/traffic", label: "Traffic", icon: "arrows" },
      { to: "/requests", label: "Requests", icon: "list" },
      { to: "/sessions", label: "Sessions", icon: "layers" },
      { to: "/settings", label: "Server settings", icon: "settings" },
      { to: "/organization", label: "Organization", icon: "users" },
      // Not /downloads: the server serves the files themselves there.
      { to: "/get", label: "Downloads", icon: "download" },
    ],
  },
  {
    id: "machines",
    number: 3,
    label: "Machines",
    summary: "Computers signed in to the server. Each runs model servers agents use (Ollama, Strata, Docker, containers), managed on its page.",
    home: "/machines",
    items: [
      { to: "/machines", label: "All machines", icon: "monitor", end: true },
    ],
  },
];

export const isGroup = (item: NavItem): item is NavGroupItem => "items" in item;

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
