import type { ActiveRequest, RequestRecord, Session, Status } from "../api";
import { formatNumber } from "../format";

interface Lane {
  name: string;
  live: number;
  detail: string;
}

const WIDTH = 920;
const ROW = 64;
const NODE_W = 230;
const NODE_H = 46;
const HUB_W = 170;

/** Agents on the left, backends on the right, every request routed through the proxy hub. */
export function RoutingMap({ status, sessions }: { status: Status; sessions: Session[] }) {
  const live = status.current_requests;
  const agents = agentLanes(sessions, status.recent_requests, live);
  const backends: Lane[] = Object.entries(status.backends).map(([name, backend]) => {
    const count = live.filter((item) => item.backend === name).length;
    return {
      name,
      live: count,
      detail: name === status.default_backend ? `${hostOf(backend.url)}, default` : hostOf(backend.url),
    };
  });
  const rows = Math.max(agents.length, backends.length, 1);
  const height = rows * ROW + 24;
  const hubY = height / 2;
  const hubX = (WIDTH - HUB_W) / 2;
  const laneY = (index: number, count: number) => height / 2 + (index - (count - 1) / 2) * ROW;

  return (
    <figure className="routing-map">
      <svg viewBox={`0 0 ${WIDTH} ${height}`} role="img" aria-labelledby="routing-title routing-desc">
        <title id="routing-title">Live request routing</title>
        <desc id="routing-desc">
          {live.length
            ? `${live.length} requests in flight. ${live.map((item) => `${item.client} to ${item.backend} using ${item.model}`).join(". ")}.`
            : "No requests in flight."}
        </desc>
        {agents.map((agent, index) => {
          const y = laneY(index, agents.length);
          return (
            <Route key={`a-${agent.name}`} from={[NODE_W, y]} to={[hubX, hubY]} live={agent.live} />
          );
        })}
        {backends.map((backend, index) => {
          const y = laneY(index, backends.length);
          return (
            <Route key={`b-${backend.name}`} from={[hubX + HUB_W, hubY]} to={[WIDTH - NODE_W, y]} live={backend.live} />
          );
        })}
        {agents.map((agent, index) => (
          <Node key={agent.name} x={0} y={laneY(index, agents.length)} lane={agent} />
        ))}
        {!agents.length && (
          <text className="map-empty" x={0} y={hubY + 4}>
            No agents yet
          </text>
        )}
        <g className={`hub ${live.length ? "is-live" : ""}`}>
          <rect x={hubX} y={hubY - 34} width={HUB_W} height={68} rx={14} />
          <text x={hubX + HUB_W / 2} y={hubY - 6} textAnchor="middle" className="hub-title">
            Proxy
          </text>
          <text x={hubX + HUB_W / 2} y={hubY + 16} textAnchor="middle" className="hub-detail">
            {live.length ? `${live.length} in flight` : "idle"}
          </text>
        </g>
        {backends.map((backend, index) => (
          <Node key={backend.name} x={WIDTH - NODE_W} y={laneY(index, backends.length)} lane={backend} />
        ))}
      </svg>
    </figure>
  );
}

function Route({ from, to, live }: { from: [number, number]; to: [number, number]; live: number }) {
  const midX = (from[0] + to[0]) / 2;
  const path = `M${from[0]},${from[1]} C${midX},${from[1]} ${midX},${to[1]} ${to[0]},${to[1]}`;
  return (
    <g className={`route ${live ? "is-live" : ""}`}>
      <path d={path} />
      {live > 0 && <path d={path} className="pulse" />}
    </g>
  );
}

function Node({ x, y, lane }: { x: number; y: number; lane: Lane }) {
  return (
    <g className={`node ${lane.live ? "is-live" : ""}`}>
      <rect x={x} y={y - NODE_H / 2} width={NODE_W} height={NODE_H} rx={10} />
      <text x={x + 14} y={y - 3} className="node-title">
        {truncate(lane.name, 22)}
      </text>
      <text x={x + 14} y={y + 14} className="node-detail">
        {truncate(lane.detail, 30)}
      </text>
      {lane.live > 0 && (
        <g>
          <circle cx={x + NODE_W - 22} cy={y} r={11} className="live-badge" />
          <text x={x + NODE_W - 22} y={y + 4} textAnchor="middle" className="live-count">
            {lane.live}
          </text>
        </g>
      )}
    </g>
  );
}

function agentLanes(sessions: Session[], recent: RequestRecord[], live: ActiveRequest[]): Lane[] {
  const names = new Map<string, { live: number; requests: number; active: boolean }>();
  const touch = (name: string) => {
    if (!names.has(name)) names.set(name, { live: 0, requests: 0, active: false });
    return names.get(name)!;
  };
  for (const item of live) touch(item.client).live++;
  for (const session of sessions) {
    if (!session.ended_at && session.request_count > 0) touch(session.client).active = true;
  }
  for (const record of recent) touch(record.client).requests++;
  return [...names.entries()]
    .sort((a, b) => b[1].live - a[1].live || Number(b[1].active) - Number(a[1].active) || b[1].requests - a[1].requests)
    .slice(0, 6)
    .map(([name, value]) => ({
      name,
      live: value.live,
      detail: value.live
        ? `${value.live} streaming now`
        : `${formatNumber(value.requests)} recent ${value.requests === 1 ? "request" : "requests"}`,
    }));
}

function hostOf(url: string) {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

const truncate = (value: string, length: number) => (value.length > length ? `${value.slice(0, length - 1)}…` : value);
