import { useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type OrgUsage, type UsageLimit, type UsageWindow } from "../api";
import { formatNumber } from "../format";
import { useToast } from "../hooks";
import { BlockedNote, capitalize, LimitMeter, tokensOf, when } from "./Providers";

const EVERYONE = "*";
const SUFFIXES: Record<string, number> = { k: 1e3, m: 1e6, b: 1e9 };

/** "1m", "10 million", "100k" or "250000" as a whole number of tokens; null if it isn't one. */
export function parseTokens(text: string): number | null {
  const match = text.trim().toLowerCase().replace(/[,_\s]/g, "").replace(/million$/, "m").replace(/thousand$/, "k").replace(/billion$/, "b").match(/^(\d+(?:\.\d+)?)([kmb]?)$/);
  if (!match) return null;
  const value = Number(match[1]) * (match[2] ? SUFFIXES[match[2]] : 1);
  return Number.isFinite(value) && Number.isInteger(Math.round(value)) ? Math.round(value) : null;
}

const personLabel = (user: string) => (user === EVERYONE ? "Everyone" : user);

/**
 * An organization's usage of each provider, by person, and the token limits its admins set (its admins
 * and site admins see everyone and change limits; members see their own rows). The server counts, decides
 * who's blocked and when it frees up; this only shows it.
 */
export function OrgUsageTab({ org }: { org: string }) {
  const usage = useQuery({ queryKey: ["org-usage", org], queryFn: () => api.orgUsage(org), refetchInterval: 15000 });
  const [window, setWindow] = useState<UsageWindow>("24h");
  const [person, setPerson] = useState("");
  const [provider, setProvider] = useState("");
  const data = usage.data;
  if (usage.isError) return <p className="bad">{usage.error.message}</p>;
  if (!data) return <p className="subtle">Loading usage…</p>;
  const labels = new Map(data.providers.map((item) => [item.id, item.label]));
  const rows = data.rows.filter((row) => (!person || row.user === person) && (!provider || row.provider === provider));
  const blocked = data.rows.filter((row) => row.blocked);
  return (
    <div className="stack">
      <p className="subtle">
        {data.manage
          ? "Tokens each person used on each provider, as this server recorded them. A limit stops that person's agents on that provider once it's reached, until the window frees up or you raise it."
          : "Your usage of each provider and the limits on you. Only this organization's admins see everyone's and set limits."}
      </p>
      {blocked.length > 0 && (
        <div className="callout">
          <strong>{blocked.length === 1 ? "1 person is" : `${blocked.length} people are`} blocked on a provider</strong>
          <span>{blocked.map((row) => `${row.user} on ${labels.get(row.provider) ?? row.provider}`).join(", ")}</span>
        </div>
      )}
      <div className="filters">
        <select value={window} onChange={(event) => setWindow(event.target.value as UsageWindow)} aria-label="Window">
          {data.windows.map((item) => <option key={item.id} value={item.id}>{capitalize(item.label)}</option>)}
        </select>
        {data.manage && (
          <select value={person} onChange={(event) => setPerson(event.target.value)} aria-label="Person">
            <option value="">Everyone</option>
            {data.users.map((user) => <option key={user} value={user}>{user}</option>)}
          </select>
        )}
        <select value={provider} onChange={(event) => setProvider(event.target.value)} aria-label="Provider">
          <option value="">Every provider</option>
          {data.providers.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
      </div>
      {rows.length ? (
        <div className="table-scroll">
          <table className="table usage-table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Provider</th>
                <th className="num">Calls</th>
                <th className="num">Tokens</th>
                <th>Limits</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.user} ${row.provider}`} className={row.blocked ? "blocked" : undefined}>
                  <td>{row.user}</td>
                  <td>{labels.get(row.provider) ?? row.provider}</td>
                  <td className="num">{formatNumber(row.usage[window].requests)}</td>
                  <td className="num">{formatNumber(tokensOf(row.usage[window]))}</td>
                  <td>
                    <div className="usage-limits">
                      {row.limits.length ? row.limits.map((limit) => <LimitMeter key={limit.window} limit={limit} windows={data.windows} />) : <span className="subtle">None</span>}
                      <BlockedNote blocked={row.blocked} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="subtle">No usage yet{person || provider ? " that matches" : ""}. It's counted from each model call agents make through this server.</p>
      )}
      {data.manage && <Limits org={org} data={data} />}
    </div>
  );
}

function Limits({ org, data }: { org: string; data: OrgUsage }) {
  const client = useQueryClient();
  const toast = useToast();
  const [user, setUser] = useState(EVERYONE);
  const [provider, setProvider] = useState("");
  const [window, setWindow] = useState<UsageWindow>("month");
  const [tokens, setTokens] = useState("");
  const parsed = parseTokens(tokens);
  const choices = useMemo(() => data.providers.filter((item) => item.kind !== "removed"), [data.providers]);
  const labels = new Map(data.providers.map((item) => [item.id, item.label]));
  const windowLabel = (id: UsageWindow) => data.windows.find((item) => item.id === id)?.label ?? id;
  const done = (report: OrgUsage) => {
    client.setQueryData(["org-usage", org], report);
    void client.invalidateQueries({ queryKey: ["providers"] });
  };
  const save = useMutation({
    mutationFn: () => api.setUsageLimit(org, { user, provider: provider || choices[0]?.id || "", window, tokens: parsed ?? -1 }),
    onSuccess: (report) => {
      done(report);
      setTokens("");
      toast("Limit saved");
    },
    onError: (error: Error) => toast(error.message),
  });
  const remove = useMutation({
    mutationFn: (limit: UsageLimit) => api.removeUsageLimit(org, { user: limit.user, provider: limit.provider, window: limit.window }),
    onSuccess: (report) => {
      done(report);
      toast("Limit removed");
    },
    onError: (error: Error) => toast(error.message),
  });
  return (
    <section className="stack">
      <h3>Limits</h3>
      <p className="subtle">
        Tokens (in and out) a person may use on a provider in a window. <strong>Everyone</strong> is each person's limit unless they have their own for
        that window; limits for different windows all apply. 0 turns the provider off for them. Agents already running finish their current call.
      </p>
      {data.limits.length ? (
        <div className="table-scroll">
          <table className="table compact">
            <thead>
              <tr>
                <th>Person</th>
                <th>Provider</th>
                <th>Window</th>
                <th className="num">Tokens</th>
                <th>Set</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {data.limits.map((limit) => (
                <tr key={`${limit.user} ${limit.provider} ${limit.window}`}>
                  <td>{personLabel(limit.user)}</td>
                  <td>{labels.get(limit.provider) ?? limit.provider}</td>
                  <td>{windowLabel(limit.window)}</td>
                  <td className="num">{limit.tokens === 0 ? "Off" : formatNumber(limit.tokens)}</td>
                  <td className="subtle">{limit.set_by ? `${limit.set_by}, ` : ""}{when(limit.set_at)}</td>
                  <td>
                    <button type="button" className="small" disabled={remove.isPending} onClick={() => remove.mutate(limit)}>Remove</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="subtle">No limits yet: everyone may use every provider as much as they like.</p>
      )}
      <form className="inline-form limit-form" onSubmit={(event: FormEvent) => (event.preventDefault(), save.mutate())}>
        <label className="field">Person
          <select value={user} onChange={(event) => setUser(event.target.value)}>
            <option value={EVERYONE}>Everyone</option>
            {data.users.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        <label className="field">Provider
          <select value={provider || choices[0]?.id || ""} onChange={(event) => setProvider(event.target.value)}>
            {choices.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <label className="field">Window
          <select value={window} onChange={(event) => setWindow(event.target.value as UsageWindow)}>
            {data.windows.map((item) => <option key={item.id} value={item.id}>{capitalize(item.label)}</option>)}
          </select>
        </label>
        <label className="field">Tokens
          <input value={tokens} onChange={(event) => setTokens(event.target.value)} placeholder="1m, 100k or 250000" inputMode="text" required
            aria-invalid={tokens !== "" && parsed === null} />
          <small>{tokens && (parsed === null ? "A number, with k, m or b if you like" : `${formatNumber(parsed)} tokens`)}</small>
        </label>
        <button type="submit" disabled={save.isPending || parsed === null || !choices.length}>Set limit</button>
      </form>
    </section>
  );
}
