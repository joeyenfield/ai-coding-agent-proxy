import { useQuery } from "@tanstack/react-query";
import { api, type SessionCost } from "../api";
import { formatUsd } from "../format";

/** A session's estimated cost, summed by the server; asked again every 15 seconds while it's shown. */
export function useSessionCost(sessionId: string, enabled = true) {
  return useQuery({
    queryKey: ["session-cost", sessionId, "watch"],
    queryFn: () => api.sessionCost(sessionId),
    enabled: enabled && Boolean(sessionId),
    retry: false,
    refetchInterval: 15_000,
  });
}

/** The figure to show, and what it means: money spent on paid APIs, else what a subscription's calls would cost at API prices. */
export function costText(cost: SessionCost): { text: string; title: string } | null {
  if (cost.priced === 0) return null;
  const spent = cost.usd > 0 || cost.subscription_usd === 0;
  const lines = cost.models.map((item) => `${item.model ?? "unknown"}: ${formatUsd(item.usd)} in ${item.requests} requests`);
  if (cost.unpriced > 0) lines.push(`${cost.unpriced} model calls had no price`);
  return {
    text: spent ? formatUsd(cost.usd) : `≈ ${formatUsd(cost.subscription_usd)}`,
    title: [spent ? "Estimated cost of this session" : "What this session would cost at API prices: a subscription doesn't bill it", ...lines].join("\n"),
  };
}

/** A session's estimated cost as a small pill, for an agent's header; nothing while it has no priced calls. */
export function SessionCostPill({ sessionId, enabled = true }: { sessionId: string; enabled?: boolean }) {
  const cost = useSessionCost(sessionId, enabled);
  const shown = cost.data ? costText(cost.data) : null;
  if (!shown) return null;
  return (
    <span className="pill cost-pill" title={shown.title}>
      {shown.text}
    </span>
  );
}
