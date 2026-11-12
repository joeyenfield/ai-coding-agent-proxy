import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ROOT_ORG, type CostRecompute, type CostTotals, type Identity, type OrgCosts } from "../api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { CostBars, CostChart } from "../components/CostChart";
import { dayAgo, DayRange } from "../components/DayRange";
import { ModelPricesSettings } from "../components/ModelPrices";
import { PartTag } from "../components/PartTag";
import { TabBar, TabPanel, useTabParam } from "../components/Tabs";
import { formatCompact, formatNumber, formatUsd } from "../format";
import { useAuth, useToast } from "../hooks";

const TABS = ["summary", "prices", "recompute"] as const;

/** The figure to lead with: money spent, or what subscriptions' calls would cost when nothing was. */
const spentText = (cost: CostTotals) => (cost.usd > 0 || cost.subscription_usd === 0 ? formatUsd(cost.usd) : `≈ ${formatUsd(cost.subscription_usd)}`);

/**
 * Costs: what an organization's model calls cost between two days, the price table they're
 * estimated with, and pricing recorded calls again. Every sum is the server's; this page only
 * shows them. An organization's admins see everyone's in it and may recompute; members see their own.
 */
export function CostsPage() {
  const identity = useAuth().data?.identity;
  if (!identity) return null;
  return <Costs identity={identity} />;
}

function Costs({ identity }: { identity: Identity }) {
  const [tab, setTab] = useTabParam(TABS);
  const [org, setOrg] = useState(identity.tenant);
  const [since, setSince] = useState(() => dayAgo(29));
  const [until, setUntil] = useState(() => dayAgo(0));
  const valid = Boolean(since && until && since <= until);
  const costs = useQuery({ queryKey: ["org-costs", org, since, until], queryFn: () => api.orgCosts(org, since, until), enabled: valid, retry: false });
  const data = costs.data;
  const manage = data?.manage ?? (identity.site_admin || (identity.role === "admin" && identity.tenant === org));
  const unpriced = (data?.models ?? []).filter((item) => item.unpriced > 0 && item.model).map((item) => item.model);

  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Costs</h1>
        <p className="lede">What model calls cost, estimated from the price table. Days are UTC days.</p>
      </header>

      <DayRange identity={identity} org={org} since={since} until={until} onOrg={setOrg} onDays={(first, last) => { setSince(first); setUntil(last); }} />

      <TabBar
        label="Costs"
        className="page-tabs"
        idPrefix="costs-"
        active={tab}
        onSelect={setTab}
        tabs={[
          { id: "summary", label: "Summary" },
          { id: "prices", label: "Model prices", badge: unpriced.length ? <span className="tab-badge live">{unpriced.length}</span> : undefined },
          { id: "recompute", label: "Recompute" },
        ]}
      />

      <TabPanel idPrefix="costs-" id="summary" active={tab === "summary"} className="cost-panel">
        {!valid ? (
          <p className="empty">Choose a first and a last day.</p>
        ) : costs.isError ? (
          <p className="bad">{costs.error.message}</p>
        ) : !data ? (
          <p className="empty">Adding up the costs…</p>
        ) : (
          <Summary data={data} onPrices={() => setTab("prices")} />
        )}
      </TabPanel>

      <TabPanel idPrefix="costs-" id="prices" active={tab === "prices"} keepMounted className="cost-panel">
        {identity.site_admin ? <ModelPricesSettings suggest={unpriced} onSaved={() => setTab("recompute")} /> : <PricesReadOnly />}
      </TabPanel>

      <TabPanel idPrefix="costs-" id="recompute" active={tab === "recompute"} className="cost-panel">
        {manage ? (
          <Recompute org={org} since={since} until={until} valid={valid} data={data} />
        ) : (
          <p className="empty">Only this organization's admins can recompute its costs.</p>
        )}
      </TabPanel>
    </div>
  );
}

function Summary({ data, onPrices }: { data: OrgCosts; onPrices: () => void }) {
  if (data.requests === 0) return <p className="empty">No model calls were recorded in these days.</p>;
  const tokens = (item: CostTotals) => `${formatCompact(item.input_tokens)} tokens in, ${formatCompact(item.output_tokens)} out`;
  return (
    <div className="stack">
      <p className="subtle">
        {data.manage ? "Everyone's model calls in this organization." : "Your own sessions' model calls. This organization's admins see everyone's."} Calls to a
        machine's own model server cost nothing here, so they're counted but have no cost.
      </p>
      <dl className="stat-cards" aria-label="Costs in these days">
        <div>
          <dt>Spent on paid APIs</dt>
          <dd>{formatUsd(data.usd)}</dd>
        </div>
        <div>
          <dt>Subscriptions, at API prices</dt>
          <dd title="What subscriptions' calls would cost at API prices: a subscription doesn't bill them">{data.subscription_usd > 0 ? `≈ ${formatUsd(data.subscription_usd)}` : formatUsd(0)}</dd>
        </div>
        <div>
          <dt>Model calls</dt>
          <dd>
            {formatNumber(data.requests)} <small>in {formatNumber(data.sessions)} sessions</small>
          </dd>
        </div>
        <div>
          <dt>With no price</dt>
          <dd>{data.unpriced > 0 ? <span className="warn">{formatNumber(data.unpriced)}</span> : "0"}</dd>
        </div>
      </dl>
      {data.unpriced > 0 && (
        <div className="callout row">
          <span>
            <strong>{formatNumber(data.unpriced)} model calls had no price</strong>, so the totals leave them out. Give their models a price, then recompute these days.
          </span>
          <button type="button" onClick={onPrices}>
            Model prices
          </button>
        </div>
      )}
      <section className="section">
        <h2>By day</h2>
        <CostChart days={data.days} />
      </section>
      <div className="cost-grid">
        <section className="section">
          <h2>By model</h2>
          <CostBars empty="No models." rows={data.models.map((item) => ({ ...item, name: item.model || "Unknown model", title: tokens(item) }))} />
        </section>
        {data.manage && (
          <section className="section">
            <h2>By person</h2>
            <CostBars empty="Nobody." rows={data.users.map((item) => ({ ...item, name: item.user || "Nobody's", title: tokens(item) }))} />
          </section>
        )}
        <section className="section">
          <h2>By provider</h2>
          <CostBars empty="No providers." rows={data.providers.map((item) => ({ ...item, name: item.provider === "direct" ? "Subscriptions (intercepted)" : item.provider || "Unknown", title: tokens(item) }))} />
        </section>
      </div>
    </div>
  );
}

/** The server's price table for those who can't change it. */
function PricesReadOnly() {
  const prices = useQuery({ queryKey: ["prices"], queryFn: api.prices });
  if (prices.isError) return <p className="bad">{prices.error.message}</p>;
  if (!prices.data) return <p className="empty">Reading the prices…</p>;
  const price = (value: number | undefined) => (value == null ? "input price" : `$${value}`);
  return (
    <section className="section">
      <p className="subtle">
        What estimates are worked out from: US dollars for a million tokens. A model takes the first row whose pattern matches its name. Site admins
        change these; an API provider's own prices, set where the provider is edited, come before them.
      </p>
      <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Model name or pattern</th>
              <th scope="col" className="num">Input</th>
              <th scope="col" className="num">Output</th>
              <th scope="col" className="num">Cache read</th>
              <th scope="col" className="num">Cache write</th>
            </tr>
          </thead>
          <tbody>
            {prices.data.prices.map((row) => (
              <tr key={row.match}>
                <td className="mono">{row.match}</td>
                <td className="num">{price(row.input)}</td>
                <td className="num">{price(row.output)}</td>
                <td className="num">{price(row.cache_read)}</td>
                <td className="num">{price(row.cache_write)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Recompute({ org, since, until, valid, data }: { org: string; since: string; until: string; valid: boolean; data: OrgCosts | undefined }) {
  const client = useQueryClient();
  const notify = useToast();
  const [confirm, setConfirm] = useState(false);
  const [done, setDone] = useState<CostRecompute | null>(null);
  const run = useMutation({
    mutationFn: () => api.recomputeCosts(org, since, until),
    onSuccess: (result) => {
      setDone(result);
      setConfirm(false);
      notify(result.changed ? `${formatNumber(result.changed)} model calls priced again` : "Nothing changed: the costs were already at these prices");
      // Every place that shows a cost reads it again.
      for (const key of ["org-costs", "session-cost", "requests", "status"]) client.invalidateQueries({ queryKey: [key] });
    },
  });
  return (
    <section className="section stack">
      <p className="subtle">
        A cost is worked out when a request is recorded, at the prices of that moment. Recomputing prices every recorded model call of this organization
        in the chosen days again, at the prices of now: calls with no price get one if there is one now, and calls priced before get today's price.
        Sessions' totals and Traffic follow. It can't be undone, except by recomputing after changing the prices back.
      </p>
      <dl className="endpoints">
        <dt>Organization</dt>
        <dd>{org === ROOT_ORG ? "Site (root)" : org}</dd>
        <dt>Days</dt>
        <dd>{valid ? `${since} to ${until} (UTC)` : "Choose a first and a last day"}</dd>
        {data && (
          <>
            <dt>Model calls</dt>
            <dd>
              {formatNumber(data.requests)}, {formatNumber(data.unpriced)} with no price
            </dd>
          </>
        )}
      </dl>
      <div className="row wrap">
        <button type="button" className="primary" disabled={!valid || run.isPending} onClick={() => { run.reset(); setConfirm(true); }}>
          Recompute costs…
        </button>
      </div>
      {done && (
        <div className="callout detected" role="status">
          <strong>
            {formatNumber(done.changed)} of {formatNumber(done.requests)} model calls changed, in {formatNumber(done.sessions)} sessions ({done.since} to {done.until})
          </strong>
          <span>
            Spent on paid APIs: {formatUsd(done.before.usd)} → {formatUsd(done.after.usd)}. Subscriptions at API prices: {formatUsd(done.before.subscription_usd)} →{" "}
            {formatUsd(done.after.subscription_usd)}. With no price: {formatNumber(done.before.unpriced)} → {formatNumber(done.after.unpriced)}.
          </span>
        </div>
      )}
      <ConfirmDialog
        open={confirm}
        title="Recompute these costs?"
        confirmLabel="Recompute"
        busy={run.isPending}
        error={run.error?.message}
        onCancel={() => setConfirm(false)}
        onConfirm={() => run.mutate()}
      >
        <p>
          Every recorded model call of {org === ROOT_ORG ? "the site organization" : org} from {since} to {until} is priced again at today's prices
          {data ? ` (${formatNumber(data.requests)} calls, now ${spentText(data)})` : ""}. The costs they have now are replaced.
        </p>
      </ConfirmDialog>
    </section>
  );
}
