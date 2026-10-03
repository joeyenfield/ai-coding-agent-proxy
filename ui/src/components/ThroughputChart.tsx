import { useState } from "react";
import { isFailure, type RequestRecord } from "../api";
import { formatDuration, formatNumber, formatRelative } from "../format";

const HEIGHT = 180;
const PAD = { top: 12, right: 12, bottom: 24, left: 44 };

/** Generation speed of recent requests, one bar per request, oldest on the left. */
export function ThroughputChart({ records }: { records: RequestRecord[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const data = records.filter((record) => record.generation_tps != null || isFailure(record)).slice(0, 48).reverse();
  if (!data.length) {
    return <p className="empty">Generation speed appears here once requests complete.</p>;
  }
  const width = 720;
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const max = niceMax(Math.max(...data.map((record) => record.generation_tps ?? 0), 1));
  const slot = plotW / data.length;
  const barW = Math.max(2, Math.min(18, slot - 2));
  const ticks = [0, max / 2, max];
  const active = hover != null ? data[hover] : null;

  return (
    <div className="chart" onMouseLeave={() => setHover(null)}>
      <svg viewBox={`0 0 ${width} ${HEIGHT}`} role="img" aria-label={`Generation speed for the last ${data.length} requests, up to ${formatNumber(max)} tokens per second`}>
        {ticks.map((tick) => {
          const y = PAD.top + plotH - (tick / max) * plotH;
          return (
            <g key={tick} className="grid">
              <line x1={PAD.left} x2={width - PAD.right} y1={y} y2={y} />
              <text x={PAD.left - 8} y={y + 4} textAnchor="end">
                {formatNumber(tick)}
              </text>
            </g>
          );
        })}
        {data.map((record, index) => {
          const x = PAD.left + index * slot + (slot - barW) / 2;
          const failed = isFailure(record);
          const value = record.generation_tps ?? 0;
          const h = failed ? 6 : Math.max(2, (value / max) * plotH);
          const y = PAD.top + plotH - h;
          return (
            <g key={`${record.session_id}:${record.request_id}`}>
              <path className={failed ? "bar failed" : hover === index ? "bar hover" : "bar"} d={roundedTop(x, y, barW, h, Math.min(4, barW / 2))} />
              <rect
                className="hit"
                x={PAD.left + index * slot}
                y={PAD.top}
                width={slot}
                height={plotH}
                onMouseEnter={() => setHover(index)}
              />
            </g>
          );
        })}
        <text className="axis-label" x={PAD.left} y={HEIGHT - 4}>
          older
        </text>
        <text className="axis-label" x={width - PAD.right} y={HEIGHT - 4} textAnchor="end">
          latest
        </text>
      </svg>
      {active && (
        <div
          className="tooltip"
          style={{ left: `${((PAD.left + (hover! + 0.5) * slot) / width) * 100}%` }}
          role="status"
        >
          <strong>{active.model || "Unknown model"}</strong>
          <span>
            {isFailure(active) ? `Failed with ${active.error_type || active.status}` : `${formatNumber(active.generation_tps)} tokens/s`}
          </span>
          <span>First token {formatDuration(active.ttft_ms)}</span>
          <span>
            {active.client} on {active.backend}, {formatRelative(active.timestamp)}
          </span>
        </div>
      )}
    </div>
  );
}

function niceMax(value: number) {
  const magnitude = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / magnitude) * magnitude;
}

function roundedTop(x: number, y: number, w: number, h: number, r: number) {
  const radius = Math.min(r, h);
  return `M${x},${y + h} V${y + radius} Q${x},${y} ${x + radius},${y} H${x + w - radius} Q${x + w},${y} ${x + w},${y + radius} V${y + h} Z`;
}
