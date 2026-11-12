import { useState } from "react";
import type { CostTotals } from "../api";
import { formatNumber, formatUsd } from "../format";

const HEIGHT = 200;
const WIDTH = 720;
const PAD = { top: 12, right: 12, bottom: 24, left: 56 };

type Day = CostTotals & { day: string };

const dayLabel = (day: string) => new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

/** A top for the axis that's a round number of dollars or cents. */
function niceMax(value: number) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return (Math.ceil(value / magnitude) * magnitude) || 1;
}

/**
 * Cost by day, one bar a day: money spent on paid APIs, with what subscriptions' calls would cost
 * at API prices stacked on it in a quieter colour, since that part isn't billed. The sums are the server's.
 */
export function CostChart({ days }: { days: Day[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const max = niceMax(Math.max(...days.map((day) => day.usd + day.subscription_usd), 0));
  const slot = plotW / Math.max(days.length, 1);
  const barW = Math.max(1.5, Math.min(28, slot - 2));
  const active = hover != null ? days[hover] : null;
  const height = (value: number) => (value > 0 ? Math.max(1.5, (value / max) * plotH) : 0);
  const total = days.reduce((sum, day) => sum + day.usd + day.subscription_usd, 0);

  return (
    <div className="chart cost-chart" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`Estimated cost by day, ${days.length} days, ${formatUsd(total)} in all`}>
        {[0, max / 2, max].map((tick) => {
          const y = PAD.top + plotH - (tick / max) * plotH;
          return (
            <g key={tick} className="grid">
              <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y} y2={y} />
              <text x={PAD.left - 8} y={y + 4} textAnchor="end">
                {tick === 0 ? "$0" : formatUsd(tick)}
              </text>
            </g>
          );
        })}
        {days.map((day, index) => {
          const x = PAD.left + index * slot + (slot - barW) / 2;
          const spent = height(day.usd);
          const atApiPrices = height(day.subscription_usd);
          const base = PAD.top + plotH;
          return (
            <g key={day.day}>
              {spent > 0 && <rect className={hover === index ? "bar hover" : "bar"} x={x} y={base - spent} width={barW} height={spent} />}
              {atApiPrices > 0 && <rect className={hover === index ? "bar unbilled hover" : "bar unbilled"} x={x} y={base - spent - atApiPrices} width={barW} height={atApiPrices} />}
              <rect className="hit" x={PAD.left + index * slot} y={PAD.top} width={slot} height={plotH} onMouseEnter={() => setHover(index)} onClick={() => setHover(index)} />
            </g>
          );
        })}
        {days.length > 0 && (
          <>
            <text className="axis-label" x={PAD.left} y={HEIGHT - 4}>
              {dayLabel(days[0].day)}
            </text>
            <text className="axis-label" x={WIDTH - PAD.right} y={HEIGHT - 4} textAnchor="end">
              {dayLabel(days[days.length - 1].day)}
            </text>
          </>
        )}
      </svg>
      {active && (
        <div className="tooltip" style={{ left: `${Math.min(82, Math.max(18, ((PAD.left + (hover! + 0.5) * slot) / WIDTH) * 100))}%` }} role="status">
          <strong>{dayLabel(active.day)}</strong>
          <span>{formatUsd(active.usd)} spent on paid APIs</span>
          {active.subscription_usd > 0 && <span>{formatUsd(active.subscription_usd)} at API prices, on subscriptions</span>}
          <span>
            {formatNumber(active.requests)} model calls{active.unpriced > 0 ? `, ${formatNumber(active.unpriced)} with no price` : ""}
          </span>
        </div>
      )}
      <ul className="chart-legend">
        <li>
          <span className="swatch" aria-hidden="true" /> Spent on paid APIs
        </li>
        <li>
          <span className="swatch unbilled" aria-hidden="true" /> Subscriptions, at API prices (not billed)
        </li>
      </ul>
    </div>
  );
}

/** A ranked list with a bar each: the same two parts as the chart, to the scale of the largest row. */
export function CostBars({ rows, empty }: { rows: (CostTotals & { name: string; title?: string })[]; empty: string }) {
  if (!rows.length) return <p className="subtle">{empty}</p>;
  const max = Math.max(...rows.map((row) => row.usd + row.subscription_usd), 0);
  const share = (value: number) => `${max > 0 ? (value / max) * 100 : 0}%`;
  return (
    <ul className="cost-bars">
      {rows.map((row) => (
        <li key={row.name} title={row.title}>
          <span className="cost-bars-name">{row.name}</span>
          <span className="cost-bars-value">
            {row.usd > 0 || row.subscription_usd === 0 ? formatUsd(row.usd) : `≈ ${formatUsd(row.subscription_usd)}`}
            {row.usd > 0 && row.subscription_usd > 0 && <small> + ≈ {formatUsd(row.subscription_usd)}</small>}
          </span>
          <span className="cost-bars-track" aria-hidden="true">
            <span className="cost-bars-fill" style={{ width: share(row.usd) }} />
            <span className="cost-bars-fill unbilled" style={{ width: share(row.subscription_usd) }} />
          </span>
          <span className="cost-bars-note subtle">
            {formatNumber(row.requests)} {row.requests === 1 ? "call" : "calls"}
            {row.unpriced > 0 && <span className="warn"> · {formatNumber(row.unpriced)} with no price</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}
