const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const compactFormat = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });

export const formatNumber = (value: number | null | undefined) =>
  value == null ? "–" : numberFormat.format(value);

export const formatCompact = (value: number | null | undefined) =>
  value == null ? "–" : compactFormat.format(value);

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export const formatDate = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString() : "–";

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "–";
  const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return new Date(value).toLocaleDateString();
}

export const shortId = (id: string) => id.slice(0, 8);

/** Drop the /session/<id> prefix the launcher adds; the session is shown separately. */
export const endpointPath = (endpoint: string | null | undefined) => (endpoint ?? "").replace(/^\/session\/[^/]+/, "") || "–";

/** When a session last carried traffic. Requests cut off by a proxy restart leave no timestamp. */
export const lastTraffic = (session: { last_activity_at: string | null; request_count: number }) =>
  session.last_activity_at ? formatRelative(session.last_activity_at) : session.request_count ? "Not recorded" : "No traffic yet";
