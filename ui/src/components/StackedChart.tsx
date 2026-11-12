import { useState } from "react";

const PAD = { top: 12, right: 12, bottom: 24, left: 56 };

export interface ChartSeries {
  key: string;
  label: string;
  /** Its place in the palette (0 to 7), or null for the rest counted together. */
  slot: number | null;
}

export interface ChartDay {
  day: string;
  /** The day's own figure: for sessions and people, each counted once, so it can be less than the bar. */
  total: number;
  values: Record<string, number>;
}

/** A series' colour: its place in the palette, which the stylesheet sets for light and dark. */
export const seriesColor = (slot: number | null) => (slot == null ? "var(--series-other)" : `var(--series-${slot + 1})`);

const dayLabel = (day: string) => new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

/** A top for the axis that's a round number. */
function niceMax(value: number, whole: boolean) {
  if (value <= 0) return whole ? 2 : 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const top = Math.ceil(value / magnitude) * magnitude || 1;
  // Counts have whole ticks: an even top keeps the middle one whole.
  return whole && top % 2 ? top + 1 : top;
}

/**
 * One bar a day, stacked by series (the first at the bottom). Pointing at a day shows its figures;
 * choosing a part of a bar picks its series, which is how the Analytics pages drill down.
 */
export function StackedChart({
  label, days, series, format, whole = false, width = 720, height = 200, onPick,
}: {
  label: string;
  days: ChartDay[];
  series: ChartSeries[];
  format: (value: number) => string;
  /** The figures are counts: the axis has no fractions. */
  whole?: boolean;
  /** The drawing's own size: a chart shown small is drawn small, so its labels stay readable. */
  width?: number;
  height?: number;
  onPick?: (key: string) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const stacked = (day: ChartDay) => series.reduce((sum, item) => sum + (day.values[item.key] ?? 0), 0);
  const max = niceMax(Math.max(...days.map(stacked), 0), whole);
  const slot = plotW / Math.max(days.length, 1);
  const barW = Math.max(1.5, Math.min(34, slot - 2));
  const active = hover != null ? days[hover] : null;
  const base = PAD.top + plotH;

  return (
    <div className="chart stacked-chart" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label} by day, ${days.length} days`}>
        {[0, max / 2, max].map((tick) => {
          const y = base - (tick / max) * plotH;
          return (
            <g key={tick} className="grid">
              <line x1={PAD.left} x2={width - PAD.right} y1={y} y2={y} />
              <text x={PAD.left - 8} y={y + 4} textAnchor="end">
                {format(tick)}
              </text>
            </g>
          );
        })}
        {days.map((day, index) => {
          const x = PAD.left + index * slot + (slot - barW) / 2;
          let top = base;
          return (
            <g key={day.day} className={hover === index ? "day hover" : "day"}>
              <rect className="hit" x={PAD.left + index * slot} y={PAD.top} width={slot} height={plotH} onMouseEnter={() => setHover(index)} onClick={() => setHover(index)} />
              {series.map((item) => {
                const value = day.values[item.key] ?? 0;
                if (value <= 0) return null;
                const size = Math.max(1.5, (value / max) * plotH);
                top -= size;
                // A hairline of the surface between the parts, where a part is tall enough to spare it.
                const gap = size > 4 ? 1 : 0;
                const pick = onPick && item.slot != null ? () => onPick(item.key) : undefined;
                return (
                  <rect
                    key={item.key}
                    className={pick ? "seg pick" : "seg"}
                    style={{ fill: seriesColor(item.slot) }}
                    x={x}
                    y={top + gap}
                    width={barW}
                    height={size - gap}
                    onMouseEnter={() => setHover(index)}
                    onClick={pick}
                  >
                    <title>{`${item.label}: ${format(value)}`}</title>
                  </rect>
                );
              })}
            </g>
          );
        })}
        {days.length > 0 && (
          <>
            <text className="axis-label" x={PAD.left} y={height - 4}>
              {dayLabel(days[0].day)}
            </text>
            <text className="axis-label" x={width - PAD.right} y={height - 4} textAnchor="end">
              {dayLabel(days[days.length - 1].day)}
            </text>
          </>
        )}
      </svg>
      {active && (
        <div className="tooltip" style={{ left: `${Math.min(80, Math.max(20, ((PAD.left + (hover! + 0.5) * slot) / width) * 100))}%` }} role="status">
          <strong>
            {dayLabel(active.day)}: {format(active.total)}
          </strong>
          {series
            .filter((item) => (active.values[item.key] ?? 0) > 0)
            .map((item) => (
              <span key={item.key} className="tooltip-row">
                <span className="swatch" style={{ background: seriesColor(item.slot) }} aria-hidden="true" />
                <span className="tooltip-name">{item.label}</span>
                <span>{format(active.values[item.key])}</span>
              </span>
            ))}
        </div>
      )}
    </div>
  );
}

/** The series' names beside their colours, once for every chart on the page. Choosing one picks it. */
export function SeriesLegend({ series, onPick }: { series: ChartSeries[]; onPick?: (key: string) => void }) {
  if (!series.length) return null;
  return (
    <ul className="chart-legend series-legend">
      {series.map((item) => (
        <li key={item.key}>
          <span className="swatch" style={{ background: seriesColor(item.slot) }} aria-hidden="true" />
          {onPick && item.slot != null ? (
            <button type="button" className="link-button" onClick={() => onPick(item.key)} title={`Only ${item.label}`}>
              {item.label}
            </button>
          ) : (
            item.label
          )}
        </li>
      ))}
    </ul>
  );
}
