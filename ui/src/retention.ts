/**
 * Labels for the retention policies (Server settings → Storage, an organization's Data tab) and the
 * audit log. The rules themselves (bounds, the audit log's floor, what's held) are the server's.
 */
import type { AuditOutcome, HoldScope, RetentionDays, RetentionKind, TraceCapture } from "./api";

export const KIND_LABELS: Record<RetentionKind, { label: string; hint: string }> = {
  traces: { label: "Traces", hint: "Full requests and responses, and the chat view of them." },
  terminal: { label: "Terminal output", hint: "What shared terminals showed." },
  records: { label: "Request records", hint: "Who called which model, tokens and timings." },
  sessions: { label: "Sessions", hint: "The session itself, and everything it recorded." },
  audit: { label: "Audit log", hint: "Who changed what. Always kept at least 90 days." },
};

export const TRACE_LABELS: Record<TraceCapture, string> = {
  off: "Never",
  opt_in: "When the session asks (--trace)",
  always: "Every session",
};

export const HOLD_LABELS: Record<HoldScope, string> = { org: "The whole organization", user: "A person", session: "A session" };

export const OUTCOME_LABELS: Record<AuditOutcome, string> = { success: "Done", refused: "Refused", failure: "Failed" };
export const OUTCOME_PILLS: Record<AuditOutcome, string> = { success: "pill good", refused: "pill bad", failure: "pill live" };

/** "Forever", "1 day" or "30 days". */
export function daysLabel(days: number | null | undefined, forever = "Forever"): string {
  if (days == null) return forever;
  return days === 1 ? "1 day" : `${days} days`;
}

/** A days field's text: empty is forever (or no bound). Returns undefined when it isn't a whole number of days. */
export function parseDays(text: string): number | null | undefined {
  const value = text.trim();
  if (!value) return null;
  if (!/^\d+$/.test(value) || Number(value) < 1) return undefined;
  return Number(value);
}

export const daysText = (days: number | null | undefined) => (days == null ? "" : String(days));

/** The bounds an organization's days for one kind must be within, in words, or null when there are none. */
export function boundsLabel(kind: RetentionKind, bounds: { min: RetentionDays; max: RetentionDays }): string | null {
  const low = bounds.min[kind];
  const high = bounds.max[kind];
  if (low && high) return `${low} to ${high} days`;
  if (low) return `At least ${daysLabel(low)}`;
  if (high) return `At most ${daysLabel(high)}`;
  return null;
}

export const when = (seconds: number | null | undefined) => (seconds ? new Date(seconds * 1000).toLocaleString() : "–");
