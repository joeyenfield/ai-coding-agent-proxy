import type { TrafficKind } from "../api";
import { useStatus } from "../hooks";
import { ROUTES, routeDetail, routeOf } from "../routes";

/** A coloured tag saying which route a request or session took: local Ollama, hosted API or an account. */
export function RouteBadge({ item }: { item: { backend: string; client?: string; kind?: TrafficKind } }) {
  const status = useStatus();
  const route = routeOf(item, status.data?.backends);
  return (
    <span className={`route-badge route-${route}`} title={`${ROUTES[route].label}: ${ROUTES[route].description}`}>
      {routeDetail(item, route)}
    </span>
  );
}
