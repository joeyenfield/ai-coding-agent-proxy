/** A labelled bar for how much of something is in use (memory, a disk), turning to the warning colour from 90%. */
export function Meter({ label, value, max, text, detail }: { label: string; value: number; max: number; text: string; detail?: string }) {
  const share = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="meter">
      <div className="meter-head">
        <span className="metric-label">{label}</span>
        <strong>{text}</strong>
      </div>
      <div className="meter-bar" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
        <span className={share >= 90 ? "high" : undefined} style={{ width: `${share}%` }} />
      </div>
      {detail && <small>{detail}</small>}
    </div>
  );
}
