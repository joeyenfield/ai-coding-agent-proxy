import { useState } from "react";

export interface Sample {
  t: number;
  v: number | null;
}

const WIDTH = 320;
const HEIGHT = 96;
const PAD = { top: 8, right: 4, bottom: 4, left: 4 };

/** A single-series line on a fixed 0-to-max scale, labelled with both bounds, with a hover crosshair. */
export function LineChart({
  label,
  samples,
  max,
  format,
  windowSeconds,
  detail,
  formatAxis,
}: {
  label: string;
  samples: Sample[];
  max: number;
  format: (value: number) => string;
  windowSeconds: number;
  detail?: string;
  /** Format for the fixed axis bounds; defaults to the value format. */
  formatAxis?: (value: number) => string;
}) {
  const formatBound = formatAxis ?? format;
  const [hover, setHover] = useState<number | null>(null);
  const now = samples.length ? samples[samples.length - 1].t : Date.now() / 1000;
  // Grow from one minute up to the full window as history accumulates.
  const span = Math.min(windowSeconds, Math.max(60, samples.length ? now - samples[0].t : 0));
  const start = now - span;
  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (t: number) => PAD.left + ((t - start) / span) * plotW;
  const y = (v: number) => PAD.top + plotH - (Math.min(v, max) / max) * plotH;
  const points = samples.filter((sample) => sample.v != null && sample.t >= start) as { t: number; v: number }[];
  const path = points.map((point, index) => `${index ? "L" : "M"}${x(point.t).toFixed(1)},${y(point.v).toFixed(1)}`).join(" ");
  const latest = points[points.length - 1];
  const active = hover != null ? points[hover] : null;

  const onMove = (event: React.PointerEvent<SVGSVGElement>) => {
    if (!points.length) return;
    const box = event.currentTarget.getBoundingClientRect();
    const t = start + ((event.clientX - box.left) / box.width) * span;
    let best = 0;
    for (let index = 1; index < points.length; index++) {
      if (Math.abs(points[index].t - t) < Math.abs(points[best].t - t)) best = index;
    }
    setHover(best);
  };

  return (
    <figure className="metric">
      <figcaption>
        <span className="metric-label">{label}</span>
        <span className="metric-value">{active ? format(active.v) : latest ? format(latest.v) : "–"}</span>
        {detail && <span className="subtle metric-detail">{detail}</span>}
      </figcaption>
      <div className="metric-body">
      <div className="metric-axis" aria-hidden="true">
        <span>{formatBound(max)}</span>
        <span>{formatBound(0)}</span>
      </div>
      <div className="metric-plot">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`${label} over the last ${Math.round(span)} seconds${latest ? `, now ${format(latest.v)}` : ""}`}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          {[0.5, 1].map((fraction) => (
            <line key={fraction} className="metric-grid" x1={PAD.left} x2={WIDTH - PAD.right} y1={y(max * fraction)} y2={y(max * fraction)} />
          ))}
          <line className="metric-base" x1={PAD.left} x2={WIDTH - PAD.right} y1={y(0)} y2={y(0)} />
          {path && <path className="metric-line" d={path} vectorEffect="non-scaling-stroke" />}
          {active && (
            <>
              <line className="metric-cross" x1={x(active.t)} x2={x(active.t)} y1={PAD.top} y2={HEIGHT - PAD.bottom} vectorEffect="non-scaling-stroke" />
            </>
          )}
        </svg>
        {active && (
          <span className="metric-dot" style={{ left: `${(x(active.t) / WIDTH) * 100}%`, top: `${(y(active.v) / HEIGHT) * 100}%` }} />
        )}
        {active && (
          <span className="metric-tip" style={{ left: `${(x(active.t) / WIDTH) * 100}%` }}>
            {format(active.v)}, {Math.max(0, Math.round(now - active.t))} s ago
          </span>
        )}
      </div>
      </div>
    </figure>
  );
}
