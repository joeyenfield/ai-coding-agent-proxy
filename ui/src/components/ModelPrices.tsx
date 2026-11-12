import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ModelPrice } from "../api";
import { useToast } from "../hooks";

/** A price being edited: text, so a half-typed number isn't lost. */
export interface PriceRow {
  key: number;
  match: string;
  input: string;
  output: string;
  cache_read: string;
  cache_write: string;
}

let nextKey = 1;
const text = (value: number | undefined) => (value == null ? "" : String(value));

export const priceRow = (price?: ModelPrice): PriceRow => ({
  key: nextKey++, match: price?.match ?? "", input: text(price?.input), output: text(price?.output), cache_read: text(price?.cache_read), cache_write: text(price?.cache_write),
});

/** A row as the server takes it, or what's wrong with it. The cache prices may be left empty. */
function priceOf(row: PriceRow): ModelPrice | string {
  const match = row.match.trim();
  if (!match) return "Give every price a model name or pattern.";
  const price: ModelPrice = { match, input: Number(row.input), output: Number(row.output) };
  if (row.input.trim() === "" || row.output.trim() === "" || !(price.input >= 0) || !(price.output >= 0)) return `${match}: input and output are prices, 0 or more.`;
  for (const rate of ["cache_read", "cache_write"] as const) {
    if (row[rate].trim() === "") continue;
    const value = Number(row[rate]);
    if (!(value >= 0)) return `${match}: ${rate.replace("_", " ")} is a price, 0 or more.`;
    price[rate] = value;
  }
  return price;
}

/** The rows as the server takes them, or the first thing wrong with one of them. */
export function pricesOf(rows: PriceRow[]): ModelPrice[] | string {
  const checked = rows.map(priceOf);
  return checked.find((item): item is string => typeof item === "string") ?? (checked as ModelPrice[]);
}

/** A price table to edit: US dollars for a million tokens, by model name or pattern, the first match first. */
export function PriceRows({ rows, onChange, empty }: { rows: PriceRow[]; onChange: (rows: PriceRow[]) => void; empty: string }) {
  const update = (key: number, patch: Partial<PriceRow>) => onChange(rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  const move = (index: number, by: number) => {
    const next = [...rows];
    const [row] = next.splice(index, 1);
    next.splice(index + by, 0, row);
    onChange(next);
  };
  const cell = (row: PriceRow, field: "input" | "output" | "cache_read" | "cache_write", label: string) => (
    <td>
      <input
        className="mono"
        inputMode="decimal"
        aria-label={`${label} for ${row.match || "a new price"}`}
        value={row[field]}
        placeholder={field.startsWith("cache") ? "input price" : "0"}
        onChange={(event) => update(row.key, { [field]: event.target.value })}
      />
    </td>
  );
  return (
    <>
      <div className="table-scroll">
        <table className="table price-table">
          <thead>
            <tr>
              <th scope="col">Model name or pattern</th>
              <th scope="col">Input</th>
              <th scope="col">Output</th>
              <th scope="col">Cache read</th>
              <th scope="col">Cache write</th>
              <th scope="col">
                <span className="visually-hidden">Order and remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={row.key}>
                <td>
                  <input className="mono" aria-label="Model name or pattern" value={row.match} placeholder="claude-sonnet-4*" onChange={(event) => update(row.key, { match: event.target.value })} />
                </td>
                {cell(row, "input", "Input price")}
                {cell(row, "output", "Output price")}
                {cell(row, "cache_read", "Cache read price")}
                {cell(row, "cache_write", "Cache write price")}
                <td className="nowrap">
                  <button type="button" className="small" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move ${row.match || "this price"} up`}>
                    ↑
                  </button>{" "}
                  <button type="button" className="small" disabled={index === rows.length - 1} onClick={() => move(index, 1)} aria-label={`Move ${row.match || "this price"} down`}>
                    ↓
                  </button>{" "}
                  <button type="button" className="small danger" onClick={() => onChange(rows.filter((item) => item.key !== row.key))}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="subtle">
                  {empty}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <button type="button" onClick={() => onChange([...rows, priceRow()])}>
        Add a price
      </button>
    </>
  );
}

/**
 * The price table the server's cost estimates are made with (Server settings, Model prices):
 * US dollars for a million tokens, by a pattern matched against the model's name, the first
 * match first. The server checks and keeps it; requests already recorded keep their cost.
 * A provider's own prices (its API providers editor) come before these.
 * suggest names models seen with no price (the Costs page): each can be added as a row.
 */
export function ModelPricesSettings({ suggest = [], onSaved }: { suggest?: string[]; onSaved?: () => void } = {}) {
  const client = useQueryClient();
  const notify = useToast();
  const prices = useQuery({ queryKey: ["model-prices"], queryFn: api.modelPrices, retry: false });
  const [rows, setRows] = useState<PriceRow[] | null>(null);
  const [problem, setProblem] = useState("");

  // Seed the editor once, and again after a save or a reset replaces the table.
  useEffect(() => {
    if (prices.data) setRows(prices.data.prices.map(priceRow));
  }, [prices.data]);

  const save = useMutation({
    mutationFn: (value: ModelPrice[] | null) => api.saveModelPrices(value),
    onSuccess: (saved, value) => {
      client.setQueryData(["model-prices"], saved);
      client.invalidateQueries({ queryKey: ["prices"] });
      onSaved?.();
      notify(value === null ? "Back to the shipped prices." : "Prices saved. They apply to requests from now on.");
    },
    onError: (error: Error) => setProblem(error.message),
  });

  if (prices.error instanceof ApiError && prices.error.status === 403) return null;
  if (prices.isError) return <p className="bad">{prices.error.message}</p>;
  if (!rows || !prices.data) return <p className="empty">Reading the prices…</p>;

  // Suggested models that still have no row of their own.
  const missing = suggest.filter((model) => model && !rows.some((row) => row.match.trim().toLowerCase() === model.toLowerCase()));

  const submit = () => {
    setProblem("");
    const checked = pricesOf(rows);
    if (typeof checked === "string") return setProblem(checked);
    save.mutate(checked);
  };

  return (
    <section className="section" aria-labelledby="model-prices">
      <div className="section-head">
        <h2 id="model-prices">Model prices</h2>
        <p className="subtle">
          What the cost estimates on sessions and requests are worked out from: US dollars for a million tokens. A model takes the first
          row whose pattern matches its name (<code>*</code> stands for anything), so put the narrower patterns first. An API provider's
          own prices, set where the provider is edited, come before these.
        </p>
      </div>
      <p className="callout">
        {prices.data.custom ? "These are this server's own prices." : "These are the prices the server ships with."} They're estimates: providers
        change their prices, so check them against your provider's price list. A model with no row gets no estimate. A change applies to
        requests from now on; ones already recorded keep the cost they were given.
      </p>
      {missing.length > 0 && (
        <div className="callout">
          <strong>{missing.length === 1 ? "1 model was used with no price" : `${missing.length} models were used with no price`}</strong>
          <div className="row wrap">
            {missing.map((model) => (
              <button key={model} type="button" className="small" onClick={() => setRows([priceRow({ match: model, input: 0, output: 0 }), ...rows])}>
                Add {model}
              </button>
            ))}
          </div>
          <span className="subtle">Each is added as a row at the top with prices of 0: fill them in from the provider's price list, then save.</span>
        </div>
      )}
      <PriceRows rows={rows} onChange={setRows} empty="No prices, so nothing gets an estimate. Add one, or go back to the shipped prices." />
      {problem && <p className="bad">{problem}</p>}
      <div className="row wrap">
        <button type="button" className="primary" disabled={save.isPending} onClick={submit}>
          {save.isPending ? "Saving…" : "Save prices"}
        </button>
        {prices.data.custom && (
          <button type="button" disabled={save.isPending} onClick={() => save.mutate(null)}>
            Back to the shipped prices
          </button>
        )}
      </div>
    </section>
  );
}
