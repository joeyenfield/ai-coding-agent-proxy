import { useQuery } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, USAGE_OTHER, type Identity, type OrgAnalytics, type UsageCell, type UsageDimension } from "../api";
import { dayAgo, DayRange, orgLabel } from "../components/DayRange";
import { PartTag } from "../components/PartTag";
import { SeriesLegend, StackedChart, seriesColor, type ChartSeries } from "../components/StackedChart";
import { formatCompact, formatDate, formatNumber, formatUsd, shortId } from "../format";
import { useAuth } from "../hooks";

type MeasureId = "sessions" | "requests" | "cost" | "people";

interface Measure {
  id: MeasureId;
  label: string;
  /** What the page about it says under its title. */
  lede: string;
  value: (cell: UsageCell) => number;
  format: (value: number) => string;
  whole: boolean;
  /** Only where the caller sees more than their own. */
  manage?: boolean;
}

const cost = (cell: UsageCell) => cell.usd + cell.subscription_usd;

const MEASURES: Measure[] = [
  { id: "sessions", label: "Sessions", lede: "Sessions that made model calls, each day.", value: (cell) => cell.sessions, format: formatNumber, whole: true },
  { id: "requests", label: "Model calls", lede: "Model calls that used tokens, each day.", value: (cell) => cell.requests, format: formatNumber, whole: true },
  { id: "cost", label: "Cost", lede: "Estimated cost each day: money spent on paid APIs, and subscriptions' calls at API prices.", value: cost, format: (value) => (value === 0 ? "$0" : formatUsd(value)), whole: false },
  { id: "people", label: "People", lede: "People whose sessions made model calls, each day.", value: (cell) => cell.users, format: formatNumber, whole: true, manage: true },
];

const DIMENSIONS: { id: UsageDimension; label: string; plural: string }[] = [
  { id: "user", label: "Person", plural: "people" },
  { id: "model", label: "Model", plural: "models" },
  { id: "provider", label: "Provider", plural: "providers" },
  { id: "agent", label: "Agent", plural: "agents" },
];

/** A value of a dimension as people read it. */
function nameOf(dimension: UsageDimension, key: string) {
  if (key === USAGE_OTHER) return "Other";
  if (key) return dimension === "provider" && key === "direct" ? "Subscriptions (intercepted)" : key;
  return { user: "Nobody's", model: "Unknown model", provider: "Unknown provider", agent: "Unknown agent" }[dimension];
}

/**
 * Analytics: an organization's usage between two days as stacked bars (sessions, model calls,
 * cost and people), split by person, model, provider or agent. Choosing a part of a split narrows
 * everything to it and splits by the next thing, which is the drill-down. /analytics is all four
 * charts; /analytics/<measure> is one of them, larger. What's chosen is in the address, so Back
 * undoes a step and a view can be linked. Every sum is the server's.
 */
export function AnalyticsPage() {
  const identity = useAuth().data?.identity;
  if (!identity) return null;
  return <Analytics identity={identity} />;
}

function Analytics({ identity }: { identity: Identity }) {
  const { measure: measureId } = useParams();
  const [params, setParams] = useSearchParams();
  const org = identity.site_admin ? params.get("org") || identity.tenant : identity.tenant;
  const since = params.get("since") || dayAgo(29);
  const until = params.get("until") || dayAgo(0);
  const manage = identity.site_admin || (identity.role === "admin" && identity.tenant === org);
  const dimensions = DIMENSIONS.filter((item) => manage || item.id !== "user");
  const filters: Partial<Record<UsageDimension, string>> = {};
  for (const item of dimensions) {
    const value = params.get(item.id);
    if (value != null) filters[item.id] = value;
  }
  const asked = params.get("by") as UsageDimension | null;
  const by = dimensions.find((item) => item.id === asked)?.id ?? dimensions.find((item) => !(item.id in filters))?.id ?? dimensions[0].id;
  const dimension = DIMENSIONS.find((item) => item.id === by)!;
  const measures = MEASURES.filter((item) => manage || !item.manage);
  const measure = measures.find((item) => item.id === measureId);
  const valid = since <= until;

  const usage = useQuery({
    queryKey: ["org-analytics", org, since, until, by, filters],
    queryFn: () => api.orgAnalytics(org, since, until, by, filters),
    enabled: valid,
    retry: false,
    placeholderData: (previous) => previous,
  });
  const data = usage.data;

  /** The address with some of what's chosen changed (null takes it away). */
  const change = (changes: Record<string, string | null>, replace = false) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value == null) next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace });
  };
  /** Narrow to one value of the split, and split by the next thing that isn't narrowed yet. */
  const drill = (key: string) => {
    const next = dimensions.find((item) => item.id !== by && !(item.id in filters));
    change({ [by]: key, by: next ? next.id : null });
  };
  const search = params.toString() ? `?${params}` : "";

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>{measure ? measure.label : "Usage"}</h1>
        <p className="lede">
          {measure ? measure.lede : "How much your organization uses its agents and models, day by day."} Choose a part of a chart to see only that. Days
          are UTC days.
        </p>
      </header>

      <DayRange
        identity={identity}
        org={org}
        since={since}
        until={until}
        // Another organization has other people, models and agents: what was chosen doesn't carry over.
        onOrg={(value) => setParams(new URLSearchParams({ org: value, since, until }))}
        onDays={(first, last) => change({ since: first, until: last }, true)}
      />

      <nav className="chip-bar" aria-label="Charts">
        <Link className="button" to={`/analytics${search}`} aria-current={measure ? undefined : "page"}>
          All charts
        </Link>
        {measures.map((item) => (
          <Link key={item.id} className="button" to={`/analytics/${item.id}${search}`} aria-current={measure?.id === item.id ? "page" : undefined}>
            {item.label}
          </Link>
        ))}
      </nav>

      <div className="usage-split">
        <div className="chip-bar" role="group" aria-label="Split by">
          <span className="subtle">Split by</span>
          {dimensions.map((item) => (
            <button key={item.id} type="button" aria-pressed={by === item.id} onClick={() => change({ by: item.id })}>
              {item.label}
            </button>
          ))}
        </div>
        {Object.keys(filters).length > 0 && (
          <div className="chip-bar" role="group" aria-label="Only">
            <span className="subtle">Only</span>
            {dimensions
              .filter((item) => item.id in filters)
              .map((item) => (
                <button key={item.id} type="button" className="usage-filter" onClick={() => change({ [item.id]: null })} title="Show all of them again">
                  {item.label}: <strong>{nameOf(item.id, filters[item.id]!)}</strong> <span aria-hidden="true">×</span>
                </button>
              ))}
            <button type="button" className="link-button" onClick={() => change(Object.fromEntries(dimensions.map((item) => [item.id, null])))}>
              Clear
            </button>
          </div>
        )}
      </div>

      {!valid ? (
        <p className="empty">Choose a first and a last day.</p>
      ) : usage.isError ? (
        <p className="bad">{usage.error.message}</p>
      ) : !data ? (
        <p className="empty">Counting…</p>
      ) : (
        <Usage data={data} measures={measures} measure={measure} dimension={dimension} search={search} narrowed={Object.keys(filters).length > 0} onPick={drill} />
      )}
    </div>
  );
}

function Usage({
  data, measures, measure, dimension, search, narrowed, onPick,
}: {
  data: OrgAnalytics;
  measures: Measure[];
  measure: Measure | undefined;
  dimension: { id: UsageDimension; label: string; plural: string };
  search: string;
  narrowed: boolean;
  onPick: (key: string) => void;
}) {
  if (data.requests === 0) {
    return <p className="empty">{narrowed ? "No model calls match what's chosen in these days." : "No model calls were recorded in these days."}</p>;
  }
  // Choosing the only value there is, of the thing already narrowed to, would change nothing.
  const pick = dimension.id in data.filters ? undefined : onPick;
  const charted = data.values.filter((item) => item.slot != null);
  const series: ChartSeries[] = charted.map((item) => ({ key: item.key, label: nameOf(dimension.id, item.key), slot: item.slot }));
  if (data.values.length > charted.length || data.more_values > 0) series.push({ key: USAGE_OTHER, label: `Other ${dimension.plural}`, slot: null });
  const days = (item: Measure) =>
    data.days.map((day) => ({ day: day.day, total: item.value(day), values: Object.fromEntries(Object.entries(day.series).map(([key, cell]) => [key, item.value(cell)])) }));
  const shown = measure ? [measure] : measures;

  return (
    <div className="stack">
      <dl className="stat-cards" aria-label="Usage in these days">
        <div>
          <dt>Sessions</dt>
          <dd>{formatNumber(data.sessions)}</dd>
        </div>
        <div>
          <dt>Model calls</dt>
          <dd>{formatNumber(data.requests)}</dd>
        </div>
        <div>
          <dt>Cost</dt>
          <dd title="Money spent on paid APIs, and what subscriptions' calls would cost at API prices, which isn't billed">
            {data.usd > 0 || data.subscription_usd === 0 ? formatUsd(data.usd) : `≈ ${formatUsd(data.subscription_usd)}`}{" "}
            {data.subscription_usd > 0 && <small>{data.usd > 0 ? `+ ≈ ${formatUsd(data.subscription_usd)}` : "at API prices"}</small>}
          </dd>
        </div>
        <div>
          {data.manage ? <dt>People</dt> : <dt>Tokens</dt>}
          <dd>{data.manage ? formatNumber(data.users) : formatCompact(data.input_tokens + data.output_tokens)}</dd>
        </div>
      </dl>

      <SeriesLegend series={series} onPick={pick} />
      <div className={measure ? "usage-charts single" : "usage-charts"}>
        {shown.map((item) => (
          <section key={item.id} className="section">
            <h2>{measure ? `${item.label} by ${dimension.label.toLowerCase()}` : <Link to={`/analytics/${item.id}${search}`}>{item.label}</Link>}</h2>
            <StackedChart label={item.label} days={days(item)} series={series} format={item.format} whole={item.whole} width={measure ? 720 : 460} height={measure ? 300 : 210} onPick={pick} />
          </section>
        ))}
      </div>
      {dimension.id !== "user" && (!measure || measure.id === "sessions" || measure.id === "people") && (
        <p className="subtle">
          A session or a person counts once in each {dimension.label.toLowerCase()} they used, so a day's bar can be taller than the day's own count. Point
          at a day for both.
        </p>
      )}

      <section className="section">
        <h2>By {dimension.label.toLowerCase()}</h2>
        <div className="table-scroll">
          <table className="table usage-table">
            <thead>
              <tr>
                <th scope="col">{dimension.label}</th>
                <th scope="col" className="num">Sessions</th>
                <th scope="col" className="num">Model calls</th>
                <th scope="col" className="num">Tokens</th>
                <th scope="col" className="num">Cost</th>
                {data.manage && <th scope="col" className="num">People</th>}
              </tr>
            </thead>
            <tbody>
              {data.values.map((item) => (
                <tr key={item.key}>
                  <th scope="row">
                    <span className="swatch" style={{ background: seriesColor(item.slot) }} aria-hidden="true" />
                    {pick ? (
                      <button type="button" className="link-button" onClick={() => pick(item.key)} title={`Only ${nameOf(dimension.id, item.key)}`}>
                        {nameOf(dimension.id, item.key)}
                      </button>
                    ) : (
                      nameOf(dimension.id, item.key)
                    )}
                  </th>
                  <td className="num">{formatNumber(item.sessions)}</td>
                  <td className="num">{formatNumber(item.requests)}</td>
                  <td className="num">{formatCompact(item.input_tokens + item.output_tokens)}</td>
                  <td className="num">{costText(item)}</td>
                  {data.manage && <td className="num">{formatNumber(item.users)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data.more_values > 0 && <p className="subtle">And {formatNumber(data.more_values)} more with less use.</p>}
      </section>

      <section className="section">
        <h2>Sessions that cost the most</h2>
        <div className="table-scroll">
          <table className="table usage-table">
            <thead>
              <tr>
                <th scope="col">Session</th>
                {data.manage && <th scope="col">Person</th>}
                <th scope="col">Agent</th>
                <th scope="col">Started</th>
                <th scope="col" className="num">Model calls</th>
                <th scope="col" className="num">Tokens</th>
                <th scope="col" className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.top_sessions.map((item) => (
                <tr key={item.session_id}>
                  <th scope="row">
                    <Link to={`/sessions/${item.session_id}`}>{item.project || shortId(item.session_id)}</Link>
                  </th>
                  {data.manage && <td>{item.user || "Nobody's"}</td>}
                  <td>{item.agent || "–"}</td>
                  <td>{formatDate(item.started_at)}</td>
                  <td className="num">{formatNumber(item.requests)}</td>
                  <td className="num">{formatCompact(item.input_tokens + item.output_tokens)}</td>
                  <td className="num">{costText(item)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="subtle">
          In {orgLabel(data.org)}, {data.since} to {data.until}. Prices and pricing recorded calls again are on <Link to="/costs">Costs</Link>.
        </p>
      </section>
    </div>
  );
}

/** Money spent, or what a subscription's calls would cost when nothing was. */
function costText(item: { usd: number; subscription_usd: number }) {
  if (item.usd > 0 && item.subscription_usd > 0) return `${formatUsd(item.usd)} + ≈ ${formatUsd(item.subscription_usd)}`;
  return item.subscription_usd > 0 ? `≈ ${formatUsd(item.subscription_usd)}` : formatUsd(item.usd);
}
