import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useModels } from "../hooks";

/** Server settings → Shared models: which of the site's providers every organization may use, and for which models. */
export function SharedModelsSettings() {
  const shares = useQuery({ queryKey: ["shared-models"], queryFn: api.sharedModels });
  const models = useModels();
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [extra, setExtra] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<{ text: string; tone: "good" | "bad" } | null>(null);
  useEffect(() => {
    if (shares.data) setDraft(shares.data.shared);
  }, [shares.data]);
  const save = useMutation({
    mutationFn: (value: Record<string, string[]>) => api.saveSharedModels(value),
    onSuccess: (result) => {
      client.setQueryData(["shared-models"], result);
      setExtra({});
      // Who can use what changed: agents' backends, model lists and providers.
      void client.invalidateQueries({ queryKey: ["agents"] });
      void client.invalidateQueries({ queryKey: ["models"] });
      void client.invalidateQueries({ queryKey: ["providers"] });
      setMessage({ text: "Saved. Every organization can use these models now.", tone: "good" });
    },
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  const toggle = (backend: string, model: string) =>
    setDraft((current) => {
      const chosen = current[backend] ?? [];
      const next = chosen.includes(model) ? chosen.filter((item) => item !== model) : [...chosen, model];
      return { ...current, [backend]: next };
    });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setMessage(null);
    const value: Record<string, string[]> = {};
    for (const backend of new Set([...Object.keys(draft), ...Object.keys(extra)])) {
      const typed = (extra[backend] ?? "").split(",").map((item) => item.trim()).filter(Boolean);
      const all = [...new Set([...(draft[backend] ?? []), ...typed])];
      if (all.length) value[backend] = all;
    }
    save.mutate(value);
  };
  const backends = shares.data?.backends ?? [];
  return (
    <section className="section" aria-labelledby="sharing-heading">
      <div className="section-head">
        <h2 id="sharing-heading">Shared with every organization</h2>
      </div>
      <p className="subtle">
        Let every organization's agents use a model on one of the site's own providers. They see the provider and the models you
        choose, never the machine it runs on or its address. Requests for any other model there are refused.
      </p>
      {shares.isError && <p className="bad">{shares.error.message}</p>}
      {shares.data && backends.length === 0 && (
        <p className="empty">No provider to share yet. Add one from a site machine, or a hosted API on API providers.</p>
      )}
      {backends.length > 0 && (
        <form className="panel" onSubmit={submit}>
          {backends.map((backend) => {
            const chosen = draft[backend.name] ?? [];
            const every = chosen.includes("*");
            const known = (models.data?.backends[backend.name]?.models ?? []).map((model) => model.name);
            const listed = [...new Set([...known, ...chosen.filter((item) => item !== "*")])].sort();
            return (
              <fieldset key={backend.name} className="form-grid">
                <legend>
                  {backend.name} <span className="subtle">({backend.type})</span>
                </legend>
                <label className="check span-2">
                  <input type="checkbox" checked={every} onChange={() => toggle(backend.name, "*")} />
                  Every model
                </label>
                {!every &&
                  listed.map((model) => (
                    <label key={model} className="check">
                      <input type="checkbox" checked={chosen.includes(model)} onChange={() => toggle(backend.name, model)} />
                      <span className="mono">{model}</span>
                    </label>
                  ))}
                {!every && (
                  <label className="field span-2">
                    Other models
                    <input
                      value={extra[backend.name] ?? ""}
                      onChange={(event) => setExtra((current) => ({ ...current, [backend.name]: event.target.value }))}
                      placeholder="Model names, separated by commas"
                      aria-label={`Other models of ${backend.name}`}
                      autoCapitalize="none"
                    />
                  </label>
                )}
              </fieldset>
            );
          })}
          <div className="row wrap">
            <button className="primary" type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save sharing"}
            </button>
            <button
              type="button"
              disabled={save.isPending || !shares.data}
              onClick={() => shares.data && (setDraft(shares.data.shared), setExtra({}), setMessage(null))}
            >
              Undo changes
            </button>
            {message && (
              <span className={message.tone} role="status">
                {message.text}
              </span>
            )}
          </div>
        </form>
      )}
    </section>
  );
}
