import { Fragment, useEffect, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError, ROOT_ORG, type AuditEvent, type AuditOutcome, type AuditQuery, type AuditVerify } from "../api";
import { PartTag } from "../components/PartTag";
import { download, useAuth, useToast } from "../hooks";
import { OUTCOME_LABELS, OUTCOME_PILLS, when } from "../retention";

const PAGE = 100;
/** The categories the server files events under (audit.py), for the type filter's suggestions. */
const CATEGORIES = ["authentication", "configuration", "accounts", "machines", "execution", "subscriptions", "usage", "data", "sessions", "interception", "api"];
const VERBS = ["create", "update", "delete", "view"];
const PREFIX = "mesh.audit.";

const shortType = (type: string) => (type.startsWith(PREFIX) ? type.slice(PREFIX.length) : type);
const targetLabel = (event: AuditEvent) => {
  const target = event.data?.target;
  if (!target || (!target.kind && !target.id)) return "";
  return [target.kind, target.id, target.part].filter(Boolean).join(" ");
};

/** A text box's value a moment after typing stops, so each key isn't a request. */
function useSettled(value: string, ms = 300): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value.trim()), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** Server › Audit log: who changed what, chained by hash, for admins (their organization's) and site admins (everyone's). */
export function AuditLogPage() {
  const identity = useAuth().data?.identity;
  const admin = Boolean(identity && (identity.site_admin || identity.role === "admin"));
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Audit log</h1>
        <p className="lede">
          Every change made through the server, and every refused attempt: who, from where, what they changed and how it ended. Each organization's
          events form a hash chain the server signs, so a changed or missing event shows when the chain is checked.
        </p>
      </header>
      {identity && !admin ? (
        <p className="empty">Only admins can read the audit log. Ask an admin of your organization.</p>
      ) : identity ? (
        <AuditEvents siteAdmin={identity.site_admin} />
      ) : null}
    </div>
  );
}

function AuditEvents({ siteAdmin }: { siteAdmin: boolean }) {
  const toast = useToast();
  const [type, setType] = useState("");
  const [actor, setActor] = useState("");
  const [outcome, setOutcome] = useState<AuditOutcome | "">("");
  const [org, setOrg] = useState("");
  const [since, setSince] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const settledType = useSettled(type);
  const settledActor = useSettled(actor);
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs, enabled: siteAdmin });
  const filters: AuditQuery = {
    type: settledType || undefined,
    actor: settledActor || undefined,
    outcome: outcome || undefined,
    org: siteAdmin && org ? org : undefined,
    // The start of that day, in this browser's time zone.
    since: since ? new Date(`${since}T00:00:00`).getTime() / 1000 : undefined,
  };
  const events = useInfiniteQuery({
    queryKey: ["audit", filters],
    queryFn: ({ pageParam }) => api.audit({ ...filters, before: pageParam, limit: PAGE }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
    retry: (count, error) => !(error instanceof ApiError && error.status === 403) && count < 1,
  });
  const [verifyOrg, setVerifyOrg] = useState<string | undefined>(undefined);
  const verify = useMutation({
    mutationFn: () => {
      setVerifyOrg(filters.org);
      return api.auditVerify(filters.org);
    },
  });
  const checkpoint = useMutation({
    mutationFn: api.auditCheckpoint,
    onSuccess: (result) => toast(result.checkpoint ? `Checkpoint signed at ${when(result.checkpoint.time)}` : "Nothing new since the last checkpoint"),
    onError: (error: Error) => toast(error.message),
  });
  const exporting = useMutation({
    mutationFn: async (format: "csv" | "ndjson") => {
      const file = await api.auditExport(filters, format);
      download(file.text, file.filename, file.type);
    },
    onError: (error: Error) => toast(error.message),
  });
  const list = events.data?.pages.flatMap((page) => page.events) ?? [];
  const filtered = Boolean(filters.type || filters.actor || filters.outcome || filters.org || filters.since);
  return (
    <>
      <div className="filters audit-filters">
        <input
          type="search"
          value={type}
          onChange={(event) => setType(event.target.value)}
          placeholder="Type, such as data.update"
          aria-label="Event type"
          list="audit-types"
          autoCapitalize="none"
          spellCheck={false}
        />
        <datalist id="audit-types">
          {CATEGORIES.map((category) => (
            <Fragment key={category}>
              <option value={category} />
              {VERBS.map((verb) => <option key={verb} value={`${category}.${verb}`} />)}
            </Fragment>
          ))}
        </datalist>
        <input type="search" value={actor} onChange={(event) => setActor(event.target.value)} placeholder="Who (username)" aria-label="Who" autoCapitalize="none" spellCheck={false} />
        <select value={outcome} onChange={(event) => setOutcome(event.target.value as AuditOutcome | "")} aria-label="Outcome">
          <option value="">Any outcome</option>
          {(Object.keys(OUTCOME_LABELS) as AuditOutcome[]).map((item) => <option key={item} value={item}>{OUTCOME_LABELS[item]}</option>)}
        </select>
        {siteAdmin && (
          <select value={org} onChange={(event) => setOrg(event.target.value)} aria-label="Organization">
            <option value="">Every organization</option>
            {orgs.data?.orgs.map((item) => <option key={item.id} value={item.id}>{item.id === ROOT_ORG ? "Site administration" : item.name}</option>)}
          </select>
        )}
        <label className="audit-since">
          <span className="subtle">Since</span>
          <input type="date" value={since} onChange={(event) => setSince(event.target.value)} aria-label="Since" />
        </label>
      </div>
      <div className="row wrap audit-actions">
        <button type="button" onClick={() => void events.refetch()} disabled={events.isFetching}>
          {events.isFetching && !events.isFetchingNextPage ? "Refreshing…" : "Refresh"}
        </button>
        <button type="button" onClick={() => exporting.mutate("csv")} disabled={exporting.isPending}>Download CSV</button>
        <button type="button" onClick={() => exporting.mutate("ndjson")} disabled={exporting.isPending}>Download NDJSON</button>
        <span className="spacer" />
        <button type="button" onClick={() => verify.mutate()} disabled={verify.isPending}>{verify.isPending ? "Checking…" : "Verify chain"}</button>
        {siteAdmin && (
          <button type="button" onClick={() => checkpoint.mutate()} disabled={checkpoint.isPending}>{checkpoint.isPending ? "Signing…" : "Sign checkpoint now"}</button>
        )}
      </div>
      <p className="subtle">Downloads have the newest 5000 events that match the filters. Times are in your browser's time zone.</p>
      {verify.isError && <p className="bad">Couldn't check the chain: {verify.error.message}</p>}
      {verify.data && <VerifyResult result={verify.data} org={verifyOrg} />}
      {events.isError && <p className="bad">Couldn't read the audit log: {events.error.message}</p>}
      {events.isPending ? (
        <p className="empty">Asking the server…</p>
      ) : !list.length ? (
        !events.isError && <p className="empty">{filtered ? "No events match." : "No events yet."}</p>
      ) : (
        <div className="table-scroll">
          <table className="table selectable audit-log">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Event</th>
                <th scope="col">Who</th>
                <th scope="col">Outcome</th>
                {siteAdmin && <th scope="col">Organization</th>}
              </tr>
            </thead>
            <tbody>
              {list.map((event) => (
                <Fragment key={event.id}>
                  <tr
                    className={open === event.id ? "selected" : undefined}
                    onClick={() => setOpen(open === event.id ? null : event.id)}
                    tabIndex={0}
                    aria-expanded={open === event.id}
                    onKeyDown={(key) => (key.key === "Enter" || key.key === " ") && (key.preventDefault(), setOpen(open === event.id ? null : event.id))}
                  >
                    <td className="mono">{when(event.time_epoch)}</td>
                    <td className="wrap">
                      <span className="mono">{shortType(event.type)}</span>
                      {event.data?.action && <div className="subtle mono">{event.data.action}</div>}
                      {targetLabel(event) && <div className="subtle">{targetLabel(event)}</div>}
                      <div className="audit-who-inline">{event.actor ?? "Not signed in"}</div>
                    </td>
                    <td className="wrap">
                      {event.actor ?? <span className="subtle">Not signed in</span>}
                      {event.data?.actor?.ip && <div className="subtle mono">{event.data.actor.ip}</div>}
                    </td>
                    <td>
                      <span className={OUTCOME_PILLS[event.outcome] ?? "pill"}>{OUTCOME_LABELS[event.outcome] ?? event.outcome}</span>
                      {event.data?.status != null && <span className="mono subtle"> {event.data.status}</span>}
                    </td>
                    {siteAdmin && <td className="mono">{event.tenant}</td>}
                  </tr>
                  {open === event.id && (
                    <tr className="audit-detail">
                      <td colSpan={siteAdmin ? 5 : 4}>
                        <dl className="access-facts">
                          {event.data?.reason && (
                            <div>
                              <dt>Reason</dt>
                              <dd className="wrap">{event.data.reason}</dd>
                            </div>
                          )}
                          {event.data?.changes?.fields?.length ? (
                            <div>
                              <dt>Changed</dt>
                              <dd className="mono wrap">{event.data.changes.fields.join(", ")}</dd>
                            </div>
                          ) : null}
                          <div>
                            <dt>Chain</dt>
                            <dd className="mono wrap">#{event.chain.seq} · {event.chain.hash.slice(0, 16)}…</dd>
                          </div>
                        </dl>
                        <pre className="code wrap audit-json">{JSON.stringify(event, null, 2)}</pre>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {events.hasNextPage && (
        <p>
          <button type="button" onClick={() => void events.fetchNextPage()} disabled={events.isFetchingNextPage}>
            {events.isFetchingNextPage ? "Loading…" : `Load ${PAGE} older`}
          </button>
        </p>
      )}
    </>
  );
}

function VerifyResult({ result, org }: { result: AuditVerify; org?: string }) {
  const tenants = Object.entries(result.tenants);
  return (
    <section className={`panel audit-verify ${result.ok ? "" : "failed"}`} aria-live="polite">
      <div className="panel-head">
        <h2>
          {result.ok ? <span className="good">The chain checks out</span> : <span className="bad">The chain has problems</span>}
        </h2>
        <p className="subtle">
          {org ? `${org}'s chain` : tenants.length === 1 ? "Your organization's chain" : `${tenants.length} organizations' chains`} recomputed from the first
          event, and {result.checkpoints === 1 ? "1 signed checkpoint" : `${result.checkpoints} signed checkpoints`} checked against them.
        </p>
      </div>
      {result.problems.length > 0 && (
        <ul className="audit-problems">
          {result.problems.map((problem) => <li key={problem} className="bad">{problem}</li>)}
        </ul>
      )}
      {tenants.length > 0 && (
        <div className="table-scroll">
          <table className="table compact">
            <thead>
              <tr>
                <th>Organization</th>
                <th className="num">Events</th>
                <th className="num">First kept</th>
                <th className="num">Head</th>
              </tr>
            </thead>
            <tbody>
              {tenants.map(([name, chain]) => (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td className="num">{chain.events}</td>
                  <td className="num">{chain.first == null ? "–" : `#${chain.first}`}</td>
                  <td className="num">{chain.head == null ? "–" : `#${chain.head}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
