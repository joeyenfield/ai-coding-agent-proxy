import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  RETENTION_KINDS,
  type CapturePolicy,
  type HoldScope,
  type LegalHold,
  type OrgRetention,
  type OrgRetentionOwn,
  type RetentionKind,
  type TraceCapture,
} from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../hooks";
import { boundsLabel, daysLabel, daysText, HOLD_LABELS, KIND_LABELS, parseDays, TRACE_LABELS, when } from "../retention";

/** Each override: the server's (not overridden), or the organization's own value. */
type DaysMode = "server" | "days" | "forever";
interface Draft {
  traces: TraceCapture | "";
  intercept_bodies: "" | "on" | "off";
  terminal_output: "" | "on" | "off";
  days: Record<RetentionKind, { mode: DaysMode; text: string }>;
}

function toDraft(policy: OrgRetention): Draft {
  const own = policy.own;
  const flag = (value: boolean | undefined) => (value === undefined ? "" : value ? "on" : "off");
  const days = Object.fromEntries(
    RETENTION_KINDS.map((kind) => {
      const value = own.days?.[kind];
      const mode: DaysMode = !own.days || !(kind in own.days) ? "server" : value == null ? "forever" : "days";
      return [kind, { mode, text: daysText(value) }];
    }),
  ) as Draft["days"];
  return { traces: own.capture?.traces ?? "", intercept_bodies: flag(own.capture?.intercept_bodies), terminal_output: flag(own.capture?.terminal_output), days };
}

/** What the organization overrides, as the server takes it, or what's wrong with a field. */
function fromDraft(draft: Draft): OrgRetentionOwn | string {
  const capture: Partial<CapturePolicy> = {};
  if (draft.traces) capture.traces = draft.traces;
  if (draft.intercept_bodies) capture.intercept_bodies = draft.intercept_bodies === "on";
  if (draft.terminal_output) capture.terminal_output = draft.terminal_output === "on";
  const days: Partial<Record<RetentionKind, number | null>> = {};
  for (const kind of RETENTION_KINDS) {
    const { mode, text } = draft.days[kind];
    if (mode === "forever") days[kind] = null;
    if (mode === "days") {
      const value = parseDays(text);
      if (!value) return `${KIND_LABELS[kind].label}: a whole number of days.`;
      days[kind] = value;
    }
  }
  return { ...(Object.keys(capture).length ? { capture } : {}), ...(Object.keys(days).length ? { days } : {}) };
}

/**
 * An organization's data: the retention policy its new sessions start under (the server's, with the
 * organization's own changes within the server's bounds), and its legal holds. For its admins and site admins.
 */
export function OrgDataTab({ org, people }: { org: string; people: string[] }) {
  const policy = useQuery({ queryKey: ["org-retention", org], queryFn: () => api.orgRetention(org) });
  return (
    <div className="stack">
      {policy.isError && <p className="bad">{policy.error.message}</p>}
      {!policy.data && !policy.isError && <p className="subtle">Loading the policy…</p>}
      {policy.data && <Effective policy={policy.data} />}
      {policy.data && <Overrides org={org} policy={policy.data} />}
      <Holds org={org} people={people} />
    </div>
  );
}

function Effective({ policy }: { policy: OrgRetention }) {
  const ownCapture = policy.own.capture ?? {};
  const ownDays = policy.own.days ?? {};
  const source = (own: boolean) => <small className="subtle">{own ? " (this organization's)" : " (the server's)"}</small>;
  return (
    <section className="panel" aria-labelledby="org-policy-heading">
      <div className="panel-head">
        <h3 id="org-policy-heading">The policy in force</h3>
        <p className="subtle">
          New sessions take this policy when they start and keep it, so a change applies to sessions started after it. Days count from a session's last
          activity.
        </p>
      </div>
      <dl className="fact-list">
        <div>
          <dt>Traces recorded</dt>
          <dd>{TRACE_LABELS[policy.capture.traces]}{source("traces" in ownCapture)}</dd>
        </div>
        <div>
          <dt>Account agents' traffic in traces</dt>
          <dd>{policy.capture.intercept_bodies ? "Yes" : "No"}{source("intercept_bodies" in ownCapture)}</dd>
        </div>
        <div>
          <dt>Terminal output recorded</dt>
          <dd>{policy.capture.terminal_output ? "Yes" : "No"}{source("terminal_output" in ownCapture)}</dd>
        </div>
        {RETENTION_KINDS.map((kind) => (
          <div key={kind}>
            <dt>{KIND_LABELS[kind].label} kept</dt>
            <dd>{daysLabel(policy.days[kind])}{source(kind in ownDays)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Overrides({ org, policy }: { org: string; policy: OrgRetention }) {
  const client = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState(() => toDraft(policy));
  const [error, setError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  useEffect(() => setDraft(toDraft(policy)), [policy]);
  const done = () => void client.invalidateQueries({ queryKey: ["org-retention", org] });
  const save = useMutation({
    mutationFn: (own: OrgRetentionOwn) => api.saveOrgRetention(org, own),
    onSuccess: () => {
      done();
      toast("Policy saved");
    },
    onError: (failure: Error) => setError(failure.message),
  });
  const reset = useMutation({
    mutationFn: () => api.resetOrgRetention(org),
    onSuccess: () => {
      setResetting(false);
      done();
      toast("Using the server's policy");
    },
  });
  const server = policy.server;
  const hasOwn = Boolean(Object.keys(policy.own.capture ?? {}).length || Object.keys(policy.own.days ?? {}).length);
  const setDays = (kind: RetentionKind, patch: Partial<Draft["days"][RetentionKind]>) =>
    setDraft((current) => ({ ...current, days: { ...current.days, [kind]: { ...current.days[kind], ...patch } } }));
  const flagLabel = (on: boolean) => (on ? "on" : "off");
  const serverDefault = (kind: RetentionKind) => {
    // Only known while not overridden: the organization's answer doesn't carry the server's own days.
    const own = policy.own.days ?? {};
    return kind in own ? undefined : policy.days[kind];
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    const own = fromDraft(draft);
    if (typeof own === "string") setError(own);
    else save.mutate(own);
  };
  return (
    <section className="panel" aria-labelledby="org-own-heading">
      <div className="panel-head">
        <h3 id="org-own-heading">This organization's changes</h3>
        <p className="subtle">Only what you change here differs from the server's policy. The server's bounds are shown under each field.</p>
      </div>
      <form className="stack" onSubmit={submit}>
        <div className="form-grid">
          <label className="field">
            Record traces
            <select value={draft.traces} onChange={(event) => setDraft({ ...draft, traces: event.target.value as Draft["traces"] })}>
              <option value="">The server's</option>
              {server.allowed_traces.map((mode) => <option key={mode} value={mode}>{TRACE_LABELS[mode]}</option>)}
            </select>
            <small>This server allows: {server.allowed_traces.map((mode) => TRACE_LABELS[mode].toLowerCase()).join(", ")}.</small>
          </label>
          <label className="field">
            Account agents' traffic in traces
            <select value={draft.intercept_bodies} onChange={(event) => setDraft({ ...draft, intercept_bodies: event.target.value as Draft["intercept_bodies"] })}>
              <option value="">The server's{"intercept_bodies" in (policy.own.capture ?? {}) ? "" : ` (${flagLabel(policy.capture.intercept_bodies)})`}</option>
              <option value="on">Recorded</option>
              <option value="off">Not recorded</option>
            </select>
          </label>
          <label className="field">
            Terminal output
            <select value={draft.terminal_output} onChange={(event) => setDraft({ ...draft, terminal_output: event.target.value as Draft["terminal_output"] })}>
              <option value="">The server's{"terminal_output" in (policy.own.capture ?? {}) ? "" : ` (${flagLabel(policy.capture.terminal_output)})`}</option>
              <option value="on">Recorded</option>
              <option value="off">Not recorded</option>
            </select>
          </label>
        </div>
        <div className="org-days">
          {RETENTION_KINDS.map((kind) => {
            const value = draft.days[kind];
            const bounds = boundsLabel(kind, server);
            const fallback = serverDefault(kind);
            return (
              <fieldset key={kind} className="field org-days-row">
                <legend>{KIND_LABELS[kind].label}</legend>
                <span className="row wrap">
                  <select value={value.mode} onChange={(event) => setDays(kind, { mode: event.target.value as DaysMode })} aria-label={`${KIND_LABELS[kind].label}: kept for`}>
                    <option value="server">The server's{fallback !== undefined ? ` (${daysLabel(fallback).toLowerCase()})` : ""}</option>
                    <option value="days">A number of days</option>
                    <option value="forever" disabled={Boolean(server.max[kind])}>Forever</option>
                  </select>
                  {value.mode === "days" && (
                    <input
                      inputMode="numeric"
                      value={value.text}
                      onChange={(event) => setDays(kind, { text: event.target.value })}
                      placeholder={server.min[kind] ? String(server.min[kind]) : "30"}
                      aria-label={`${KIND_LABELS[kind].label}: days`}
                      aria-invalid={value.text !== "" && !parseDays(value.text)}
                      required
                    />
                  )}
                </span>
                <small>{bounds ? `This server allows ${bounds.charAt(0).toLowerCase()}${bounds.slice(1)}.` : "Any number of days, or forever."}</small>
              </fieldset>
            );
          })}
        </div>
        {error && <p className="bad" role="alert">{error}</p>}
        <div className="row wrap">
          <button className="primary" type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save changes"}</button>
          <button type="button" onClick={() => setResetting(true)} disabled={!hasOwn}>Use the server's</button>
        </div>
      </form>
      <ConfirmDialog
        open={resetting}
        title="Use the server's policy?"
        confirmLabel="Use the server's"
        busy={reset.isPending}
        error={reset.error?.message}
        onConfirm={() => reset.mutate()}
        onCancel={() => setResetting(false)}
      >
        This organization's changes are removed. Sessions started from now on follow the server's policy; running and past sessions keep the one they started under.
      </ConfirmDialog>
    </section>
  );
}

function Holds({ org, people }: { org: string; people: string[] }) {
  const holds = useQuery({ queryKey: ["holds", org], queryFn: () => api.holds(org) });
  const client = useQueryClient();
  const toast = useToast();
  const [scope, setScope] = useState<HoldScope>("user");
  const [target, setTarget] = useState("");
  const [reason, setReason] = useState("");
  const [lifting, setLifting] = useState<LegalHold | null>(null);
  const changed = () => {
    void client.invalidateQueries({ queryKey: ["holds", org] });
    void client.invalidateQueries({ queryKey: ["org-retention", org] });
  };
  const add = useMutation({
    mutationFn: () => api.addHold({ scope, target: scope === "org" ? org : target.trim(), reason: reason.trim(), org }),
    onSuccess: () => {
      changed();
      setTarget("");
      setReason("");
      toast("Hold placed");
    },
    onError: (error: Error) => toast(error.message),
  });
  const lift = useMutation({
    mutationFn: (id: string) => api.removeHold(id),
    onSuccess: () => {
      setLifting(null);
      changed();
      toast("Hold lifted");
    },
  });
  const list = (holds.data?.holds ?? []).filter((hold) => hold.tenant === org);
  const describe = (hold: LegalHold) => (hold.scope === "org" ? "The whole organization" : hold.scope === "user" ? `${hold.target}'s sessions` : `Session ${hold.target}`);
  return (
    <section className="panel" aria-labelledby="org-holds-heading">
      <div className="panel-head">
        <h3 id="org-holds-heading">Legal holds</h3>
        <p className="subtle">
          While a hold is in place, nothing it covers is deleted by any policy: sessions, their recordings and, for a hold on the organization, its audit
          log. Lifting it lets the policy catch up at its next hourly run.
        </p>
      </div>
      {holds.isError && <p className="bad">{holds.error.message}</p>}
      {list.length ? (
        <div className="table-scroll">
          <table className="table compact holds-table">
            <thead>
              <tr>
                <th>Holds</th>
                <th>Reason</th>
                <th>Placed</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {list.map((hold) => (
                <tr key={hold.id}>
                  <td className="wrap">
                    <strong>{describe(hold)}</strong>
                    <div className="subtle">{HOLD_LABELS[hold.scope]}</div>
                  </td>
                  <td className="wrap">{hold.reason || <span className="subtle">None given</span>}</td>
                  <td className="subtle">{hold.set_by ? `${hold.set_by}, ` : ""}{when(hold.set_at)}</td>
                  <td>
                    <button type="button" className="small danger" onClick={() => setLifting(hold)}>Lift</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        holds.data && <p className="subtle">No holds: the policy deletes what's past its time.</p>
      )}
      <form className="hold-form" onSubmit={(event: FormEvent) => (event.preventDefault(), add.mutate())}>
        <h4>Place a hold</h4>
        <div className="form-grid">
          <label className="field">
            Hold
            <select value={scope} onChange={(event) => (setScope(event.target.value as HoldScope), setTarget(""))}>
              <option value="user">{HOLD_LABELS.user}</option>
              <option value="session">{HOLD_LABELS.session}</option>
              <option value="org">{HOLD_LABELS.org}</option>
            </select>
          </label>
          {scope === "user" && (
            <label className="field">
              Person
              <select value={target} onChange={(event) => setTarget(event.target.value)} required>
                <option value="">Choose someone…</option>
                {people.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <small>Every session they started or own.</small>
            </label>
          )}
          {scope === "session" && (
            <label className="field">
              Session id
              <input value={target} onChange={(event) => setTarget(event.target.value)} required maxLength={200} autoCapitalize="none" spellCheck={false} />
              <small>From the session's page, under Sessions.</small>
            </label>
          )}
          {scope === "org" && (
            <div className="field">
              Organization
              <input value={org} disabled />
              <small>Everything of this organization's, its audit log too.</small>
            </div>
          )}
          <label className="field span-2">
            Reason
            <textarea rows={2} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={2000} placeholder="A case number, or why it's kept" />
          </label>
        </div>
        <button type="submit" disabled={add.isPending}>{add.isPending ? "Placing…" : "Place hold"}</button>
      </form>
      <ConfirmDialog
        open={lifting !== null}
        title="Lift this hold?"
        confirmLabel="Lift hold"
        busy={lift.isPending}
        error={lift.error?.message}
        onConfirm={() => lifting && lift.mutate(lifting.id)}
        onCancel={() => setLifting(null)}
      >
        {lifting && describe(lifting)} will follow the retention policy again: anything past its time is deleted at the next hourly run, and that can't be undone.
      </ConfirmDialog>
    </section>
  );
}
