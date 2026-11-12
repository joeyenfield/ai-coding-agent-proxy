/**
 * The console's navigation.
 *
 * Two views of the same pages:
 *
 * - The three parts a request passes through, in the order it flows: agents (1) start it, the
 *   server (3) records it and routes it, and a machine (2) answers it with a model server it runs.
 *   Only hosted APIs and agents' own accounts aren't on a machine. The Overview and its routing
 *   map explain the console this way.
 * - The rail's sections, grouped by what you're doing: running agents, the models they use, the
 *   machines, the traffic, the analytics, the logs and the server's administration. The breadcrumb above each
 *   page's title names its section.
 *
 * Everything about a model server (its models, settings, logs, the containers that run it, the
 * machine's load) is on its machine's page (/machines/<id>), because the machine does it.
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
  /** One line on what the page is for: the command menu's hint. */
  hint?: string;
}

/** One of the three parts a request passes through. */
export interface Part {
  id: PartId;
  number: number;
  label: string;
  /** One line on what the part is for. */
  summary: string;
  /** Its main pages, linked from its card on the Overview. */
  links: NavLinkItem[];
}

/** A heading in the rail and the pages under it. */
export interface NavSection {
  id: string;
  label: string;
  /** One line on what the section is for: its heading's tooltip. */
  summary: string;
  items: NavLinkItem[];
}

const PAGES = {
  liveAgents: { to: "/live-agents", label: "Live agent view", icon: "grid", hint: "Several agents' terminals at once" },
  terminals: { to: "/terminals", label: "Running agents", icon: "terminal", hint: "Agents running now: who, what they're doing, their traffic" },
  agents: { to: "/agents", label: "Start an agent", icon: "play", hint: "On a machine, or the command to run it yourself" },
  ask: { to: "/ask", label: "Ask a model", icon: "chat", hint: "Chat with one model through the server" },
  providers: { to: "/providers", label: "Providers", icon: "cube", hint: "Where agents' calls go, your usage and limits" },
  subscriptions: { to: "/subscriptions", label: "Subscriptions", icon: "key", hint: "Claude, Codex and Copilot plans" },
  shared: { to: "/shared", label: "Shared with you", icon: "arrows", hint: "What linked servers' organizations share with yours" },
  machines: { to: "/machines", label: "All machines", icon: "monitor", hint: "The fleet: computers signed in to the server" },
  addMachine: { to: "/machines#add", label: "Add a machine", icon: "plus", hint: "One command to put a computer in the fleet" },
  approve: { to: "/approve-device", label: "Approve a device", icon: "lock", hint: "Sign in agent login or agent-machine login elsewhere" },
  live: { to: "/live", label: "Live", icon: "activity", hint: "Model calls as they stream" },
  traffic: { to: "/traffic", label: "Traffic", icon: "arrows", hint: "Every request, grouped by what it was for" },
  requests: { to: "/requests", label: "Requests", icon: "zap", hint: "Model calls, with their payloads" },
  sessions: { to: "/sessions", label: "Sessions", icon: "layers", hint: "Each agent run, its sharing and recordings" },
  usage: { to: "/analytics", label: "Usage", icon: "activity", end: true, hint: "Sessions, model calls, cost and people by day, in one place" },
  usageSessions: { to: "/analytics/sessions", label: "Sessions by day", icon: "layers", hint: "Sessions each day, by person, model, provider or agent" },
  usageRequests: { to: "/analytics/requests", label: "Model calls by day", icon: "zap", hint: "Model calls each day, by person, model, provider or agent" },
  usageCost: { to: "/analytics/cost", label: "Cost by day", icon: "chart", hint: "Estimated cost each day, by person, model, provider or agent" },
  usagePeople: { to: "/analytics/people", label: "People by day", icon: "users", admin: true, hint: "Who used agents each day" },
  costs: { to: "/costs", label: "Costs and prices", icon: "list", hint: "What model calls cost in all; the price table; recompute" },
  accessLog: { to: "/access-log", label: "Access log", icon: "list", siteAdmin: true, hint: "Every HTTP request and WebSocket, the refused ones too" },
  apiLog: { to: "/api-log", label: "API log", icon: "book", siteAdmin: true, hint: "Saved requests, with payloads in debug mode" },
  audit: { to: "/audit", label: "Audit log", icon: "shield", admin: true, hint: "Who changed what, chained by hash" },
  organizations: { to: "/organization", label: "Organizations", icon: "users", hint: "People, teams, providers, limits and data" },
  settings: { to: "/settings", label: "Server settings", icon: "settings", siteAdmin: true, hint: "Providers, agents, sharing, storage, links" },
  stats: { to: "/server-stats", label: "Server stats", icon: "cpu", siteAdmin: true, hint: "The server's load and storage" },
  downloads: { to: "/get", label: "Downloads", icon: "download", hint: "agent, agent-machine and the Android app" },
  docs: { to: "/help", label: "Documentation", icon: "help", hint: "How everything works" },
} satisfies Record<string, NavLinkItem>;

export const SECTIONS: NavSection[] = [
  {
    id: "agents",
    label: "Agents",
    summary: "Start coding agents through the server, then watch them and type into them from any device.",
    items: [PAGES.agents, PAGES.liveAgents, PAGES.terminals, PAGES.ask],
  },
  {
    id: "models",
    label: "Models",
    summary: "Where agents' model calls go: machines' model servers, hosted APIs and your own subscriptions.",
    items: [PAGES.providers, PAGES.subscriptions, PAGES.shared],
  },
  {
    id: "machines",
    label: "Machines",
    summary: "Computers signed in to the server. Each runs model servers, containers and agents, managed on its page.",
    items: [PAGES.machines, PAGES.addMachine, PAGES.approve],
  },
  {
    id: "activity",
    label: "Activity",
    summary: "What agents send through the server, as it happens and afterwards.",
    items: [PAGES.live, PAGES.traffic, PAGES.requests, PAGES.sessions],
  },
  {
    id: "analytics",
    label: "Analytics",
    summary: "How much your organization uses its agents and models, day by day, and what it costs.",
    items: [PAGES.usage, PAGES.usageSessions, PAGES.usageRequests, PAGES.usageCost, PAGES.usagePeople, PAGES.costs],
  },
  {
    id: "logs",
    label: "Logs",
    summary: "What reached the server, what it saved and who changed what.",
    items: [PAGES.accessLog, PAGES.apiLog, PAGES.audit],
  },
  {
    id: "admin",
    label: "Administration",
    summary: "Organizations, and the server's own settings and load.",
    items: [PAGES.organizations, PAGES.settings, PAGES.stats],
  },
];

/** Pages at the foot of the rail, for everyone. */
export const HELP_LINKS: NavLinkItem[] = [PAGES.downloads, PAGES.docs];

/** The three parts, in the order a request flows on the routing map: agents, the server, machines. */
export const PARTS: Part[] = [
  {
    id: "agents",
    number: 1,
    label: "Agents",
    summary: "Start a coding agent through the server, then watch it and type into it from any device.",
    links: [PAGES.agents, PAGES.terminals, PAGES.providers],
  },
  {
    id: "machines",
    number: 2,
    label: "Machines",
    summary: "Computers signed in to the server. Each runs model servers agents use (Ollama, Strata, Docker, containers), managed on its page.",
    links: [PAGES.machines, PAGES.addMachine],
  },
  {
    id: "server",
    number: 3,
    label: "Server",
    summary: "The hub: every request agents make passes through it and is recorded, on its way to a machine.",
    links: [PAGES.live, PAGES.traffic, PAGES.sessions],
  },
];

/** Parts by id, whatever their order (the routing map draws them in the order a request flows). */
export function partsById(...ids: PartId[]): Part[] {
  return ids.map((id) => PARTS.find((part) => part.id === id) as Part);
}

const allowedFor = (siteAdmin: boolean, admin: boolean) => (link: NavLinkItem) => siteAdmin || (!link.siteAdmin && (admin || !link.admin));

/** The rail's sections with only the pages this person can use; a section left empty is dropped. */
export function visibleSections(siteAdmin: boolean, admin = siteAdmin): NavSection[] {
  const allowed = allowedFor(siteAdmin, admin);
  return SECTIONS.map((section) => ({ ...section, items: section.items.filter(allowed) })).filter((section) => section.items.length > 0);
}

/** Every page this person can reach from the rail, with its section: what the command menu searches. */
export function visiblePages(siteAdmin: boolean, admin = siteAdmin): { page: NavLinkItem; section: string }[] {
  return [
    ...visibleSections(siteAdmin, admin).flatMap((section) => section.items.map((page) => ({ page, section: section.label }))),
    ...HELP_LINKS.map((page) => ({ page, section: "Help" })),
  ];
}

const pathOf = (to: string) => to.split("#")[0];

/** Whether a rail link is the page you're on. A link to a part of a page (#add) never is: its page's own link is. */
export function linkActive(link: NavLinkItem, pathname: string): boolean {
  if (link.to.includes("#")) return false;
  return pathname === link.to || (!link.end && pathname.startsWith(`${link.to}/`));
}

/** The section a page belongs to (for the breadcrumb), or null for the Overview and pages outside the rail. */
export function sectionFor(pathname: string): { section: NavSection; page: NavLinkItem } | null {
  for (const section of SECTIONS) {
    for (const page of section.items) {
      const to = pathOf(page.to);
      // A page with pages under it (end) is only itself: /analytics/cost is Cost by day, not Usage.
      if (pathname === to || (!page.end && pathname.startsWith(`${to}/`))) return { section, page };
    }
  }
  const help = HELP_LINKS.find((page) => pathname === page.to || pathname.startsWith(`${page.to}/`));
  return help ? { section: { id: "help", label: "Help", summary: "", items: HELP_LINKS }, page: help } : null;
}
