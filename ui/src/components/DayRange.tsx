import { useQuery } from "@tanstack/react-query";
import { api, ROOT_ORG, type Identity } from "../api";

const RANGES = [7, 30, 90];

/** A UTC day, days back from today: the server's days are UTC days, as its records' timestamps are. */
export const dayAgo = (days: number) => new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);

export const orgLabel = (org: string, name?: string) => (org === ROOT_ORG ? "Site (root)" : name || org);

/**
 * The organization (site admins choose; everyone else has their own) and the first and last day
 * that Costs and the Analytics pages add things up between.
 */
export function DayRange({
  identity, org, since, until, onOrg, onDays,
}: {
  identity: Identity;
  org: string;
  since: string;
  until: string;
  onOrg: (org: string) => void;
  onDays: (since: string, until: string) => void;
}) {
  // Site admins look after every organization: they choose which one's to see.
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs, enabled: identity.site_admin });
  const preset = RANGES.find((days) => until === dayAgo(0) && since === dayAgo(days - 1));
  return (
    <div className="filters cost-range">
      {identity.site_admin && (
        <select value={org} onChange={(event) => onOrg(event.target.value)} aria-label="Organization">
          {(orgs.data?.orgs ?? [{ id: org, name: org }]).map((item) => (
            <option key={item.id} value={item.id}>
              {orgLabel(item.id, item.name)}
            </option>
          ))}
        </select>
      )}
      <label>
        From <input type="date" value={since} max={until} onChange={(event) => onDays(event.target.value, until)} />
      </label>
      <label>
        to <input type="date" value={until} min={since} max={dayAgo(0)} onChange={(event) => onDays(since, event.target.value)} />
      </label>
      <div className="chip-bar" role="group" aria-label="Range">
        {RANGES.map((days) => (
          <button key={days} type="button" aria-pressed={preset === days} onClick={() => onDays(dayAgo(days - 1), dayAgo(0))}>
            {days} days
          </button>
        ))}
      </div>
    </div>
  );
}
