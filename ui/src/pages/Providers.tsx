import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type LimitStatus, type ProviderWithUsage, type UsageWindow, type UsageWindowInfo } from "../api";
import { PartTag } from "../components/PartTag";
import { StateDot } from "../components/ProviderState";
import { formatNumber } from "../format";
import { useAuth } from "../hooks";

const GROUPS: { kind: ProviderWithUsage["kind"]; title: string; summary: string }[] = [
  { kind: "local", title: "On machines", summary: "Model servers the fleet's machines run: Ollama, llama.cpp, Strata, Docker Model Runner and containers." },
  { kind: "hosted", title: "Hosted APIs", summary: "Pay-as-you-go APIs and gateways, such as OpenRouter or a LiteLLM Proxy, that the server passes agents' calls to: with your organization's key, or the agent's own." },
  { kind: "subscription", title: "Subscriptions", summary: "Account agents signed in to a Claude, ChatGPT (Codex) or Copilot plan. Calls to the provider's own hosts count here, whichever plan carried them." },
];

export const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

export const calls = (count: number) => `${formatNumber(count)} ${count === 1 ? "call" : "calls"}`;

export const when = (at: number | null | undefined) => (at ? new Date(at * 1000).toLocaleString() : "");

/** Tokens a call used: what the provider reported, in and out. */
export const tokensOf = (counts: { input_tokens: number; output_tokens: number } | undefined) => (counts ? counts.input_tokens + counts.output_tokens : 0);

/** One limit as a bar: what's used of it, in its window. */
export function LimitMeter({ limit, windows }: { limit: LimitStatus; windows: UsageWindowInfo[] }) {
  const label = windows.find((item) => item.id === limit.window)?.label ?? limit.window;
  return (
    <div className="limit-meter">
      <div className="meter-head">
        <span>
          {limit.tokens === 0 ? "Turned off" : `${formatNumber(limit.tokens)} tokens`} in {label}
          {limit.inherited && <span className="subtle"> · everyone's limit</span>}
        </span>
        <strong>{limit.tokens === 0 ? "" : `${Math.min(limit.percent_used, 999)}%`}</strong>
      </div>
      <div className="meter-bar" aria-hidden>
        <span className={limit.reached ? "over" : limit.percent_used >= 75 ? "high" : undefined} style={{ width: `${Math.min(100, limit.percent_used)}%` }} />
      </div>
      <span className="subtle">
        {formatNumber(limit.used)} used{limit.reached ? "." : `, ${formatNumber(limit.remaining)} left.`}
      </span>
    </div>
  );
}

export function BlockedNote({ blocked }: { blocked: LimitStatus | null }) {
  if (!blocked) return null;
  return (
    <p className="warn">
      Blocked: the usage limit is reached{blocked.until ? ` until ${when(blocked.until)}` : ""}. Agents' calls here are refused until then, or until an admin raises it.
    </p>
  );
}

/**
 * Providers: everywhere agents' model calls go, as one list: model servers on machines, hosted APIs and
 * kinds of subscription. Each shows your usage and your organization's limits on you there; a provider
 * whose limit you've reached refuses your agents (the server decides, the console only shows it).
 */
export function ProvidersPage() {
  const { data: auth } = useAuth();
  const identity = auth?.identity;
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => api.providers(), refetchInterval: 15000 });
  const [window, setWindow] = useState<UsageWindow>("24h");
  const data = providers.data;
  const windows = data?.windows ?? [];
  const blocked = data?.providers.filter((item) => item.blocked) ?? [];
  const orgLink = identity ? `/organization?org=${encodeURIComponent(identity.tenant)}` : "/organization";
  const usageLink = identity ? `${orgLink}&orgTab=usage` : orgLink;
  // Its admins (and site admins) set up the organization's API providers: OpenRouter, a LiteLLM Proxy, ...
  const providersLink = identity ? `${orgLink}&orgTab=providers` : orgLink;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Providers</h1>
        <p className="lede">
          Everywhere your agents' model calls can go. Your organization can limit how many tokens each person uses on each provider; once you reach a
          limit, that provider refuses your agents until it frees up.
        </p>
      </header>
      <div className="filters">
        <select value={window} onChange={(event) => setWindow(event.target.value as UsageWindow)} aria-label="Usage in">
          {windows.map((item) => <option key={item.id} value={item.id}>{capitalize(item.label)}</option>)}
        </select>
        <Link to={usageLink}>{identity?.role === "admin" ? "Everyone's usage and limits" : "Your organization's usage"}</Link>
        <Link to={orgLink}>Your organization</Link>
      </div>
      {providers.isError && <p className="bad">Couldn't load the providers: {String(providers.error)}</p>}
      {blocked.length > 0 && (
        <div className="callout">
          <strong>You've reached a limit on {blocked.map((item) => item.label).join(", ")}.</strong>
          <span>Start agents on another provider, or ask an admin of your organization to raise it.</span>
        </div>
      )}
      {GROUPS.map((group) => {
        const items = data?.providers.filter((item) => item.kind === group.kind) ?? [];
        // Admins see Hosted APIs even when there are none: it's where they set them up.
        const manage = group.kind === "hosted" && identity?.role === "admin";
        if (!items.length && group.kind !== "local" && !manage) return null;
        return (
          <section className="section" key={group.kind}>
            <h2>{group.title}</h2>
            <p className="subtle">
              {group.summary}
              {manage && (
                <>
                  {" "}Add and change your organization's, and choose the teams that may use each, on its{" "}
                  <Link to={providersLink}>API providers</Link> page.
                </>
              )}
            </p>
            {items.length ? (
              <div className="provider-usage-grid">
                {items.map((item) => <ProviderCard key={item.id} provider={item} window={window} windows={windows} />)}
              </div>
            ) : data && group.kind === "hosted" ? (
              <p className="subtle">No hosted APIs yet.</p>
            ) : data ? (
              <p className="subtle">
                No model servers on your organization's machines yet. <Link to="/machines">Machines</Link> run them; add one there with <strong>Add as a provider</strong>.
              </p>
            ) : null}
          </section>
        );
      })}
    </div>
  );
}

function ProviderCard({ provider, window, windows }: { provider: ProviderWithUsage; window: UsageWindow; windows: UsageWindowInfo[] }) {
  const counts = provider.usage[window];
  return (
    <div className={`meter provider-usage${provider.blocked ? " blocked" : ""}`}>
      <div className="meter-head">
        <strong className="provider-usage-name">{provider.label}</strong>
        <span className="pill">{provider.kind === "subscription" ? "Subscription" : provider.type_label}</span>
      </div>
      <span className="subtle row">
        {provider.machine ? (
          <>
            <StateDot tone={provider.machine.online ? "good" : "idle"} />
            <Link to={`/machines/${encodeURIComponent(provider.machine.id)}/providers`}>{provider.machine.name}</Link>
            {provider.machine.online ? "" : " · offline"}
          </>
        ) : provider.kind === "subscription" ? (
          <span>
            {provider.accounts ? `${provider.accounts} of your subscriptions` : "None of yours on the server"} ·{" "}
            <Link to="/subscriptions">Subscriptions</Link>
          </span>
        ) : (
          "Hosted"
        )}
        {provider.default && <span className="pill">Default</span>}
        {provider.container && <span className="pill">Container</span>}
      </span>
      <div className="provider-usage-counts">
        <span><strong>{formatNumber(tokensOf(counts))}</strong> tokens</span>
        <span className="subtle">{calls(counts.requests)} · {formatNumber(counts.input_tokens)} in, {formatNumber(counts.output_tokens)} out</span>
      </div>
      {provider.limits.length ? (
        provider.limits.map((limit) => <LimitMeter key={limit.window} limit={limit} windows={windows} />)
      ) : (
        <span className="subtle">No limit on you here.</span>
      )}
      <BlockedNote blocked={provider.blocked} />
    </div>
  );
}
