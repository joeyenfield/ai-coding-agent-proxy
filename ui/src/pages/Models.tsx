import { useState } from "react";
import type { OllamaModel, Profile } from "../api";
import { formatBytes, formatNumber } from "../format";
import { useModels } from "../hooks";

export function ModelsPage() {
  const models = useModels();
  const [query, setQuery] = useState("");
  const [loadedOnly, setLoadedOnly] = useState(false);
  const text = query.trim().toLowerCase();
  const filter = (model: OllamaModel) => (!text || model.name.toLowerCase().includes(text)) && (!loadedOnly || model.loaded);
  return (
    <div className="page wide">
      <header className="page-head">
        <h1>Models</h1>
        <p className="lede">
          Models on each Ollama backend, and the settings the proxy adds before forwarding. Edit them in{" "}
          <code>config/models.yaml</code>.
        </p>
      </header>
      <div className="filters">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter models" aria-label="Filter models" />
        <label className="check">
          <input type="checkbox" checked={loadedOnly} onChange={(event) => setLoadedOnly(event.target.checked)} />
          Loaded in memory only
        </label>
      </div>
      {models.isError && <p className="bad">{models.error.message}</p>}
      {models.isPending && <p className="empty">Asking each backend for its models…</p>}
      {models.data &&
        Object.entries(models.data.backends).map(([name, backend]) => {
          const visible = backend.models.filter(filter);
          return (
            <section key={name} className="section">
              <div className="section-head">
                <h2>{name}</h2>
                <p className={backend.online ? "subtle" : "bad"}>
                  {backend.online
                    ? `${backend.url}, Ollama ${backend.version}, ${backend.models.length} models, ${backend.models.filter((model) => model.loaded).length} loaded`
                    : `Offline: ${backend.error}`}
                </p>
              </div>
              {backend.online && (
                <div className="table-scroll">
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">Model</th>
                        <th scope="col" className="num">Size</th>
                        <th scope="col">Memory</th>
                        <th scope="col">Proxy settings</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((model) => (
                        <tr key={model.name}>
                          <td>
                            <strong className="mono">{model.name}</strong>
                            <small>
                              {[model.family, model.parameter_size, model.quantization].filter(Boolean).join(", ")}
                            </small>
                          </td>
                          <td className="num nowrap">{formatBytes(model.size)}</td>
                          <td>
                            {model.loaded ? (
                              <GpuShare model={model} />
                            ) : (
                              <span className="subtle">Not loaded</span>
                            )}
                          </td>
                          <td>
                            <ProfileSummary profile={model.profile} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!visible.length && <p className="empty">No models match.</p>}
                </div>
              )}
            </section>
          );
        })}
      {models.data && Object.keys(models.data.aliases).length > 0 && (
        <section className="section">
          <h2>Aliases</h2>
          <p className="subtle">Names agents can use in place of an Ollama tag.</p>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Alias</th>
                <th scope="col">Ollama model</th>
                <th scope="col">Proxy settings</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(models.data.aliases).map(([alias, profile]) => (
                <tr key={alias}>
                  <td className="mono">{alias}</td>
                  <td className="mono">{profile.ollama_model}</td>
                  <td>
                    <ProfileSummary profile={profile} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

/** Ollama reports the loaded size and the part of it in VRAM; anything else runs on the CPU. */
function GpuShare({ model }: { model: OllamaModel }) {
  const total = model.size_loaded ?? 0;
  const share = total && model.size_vram != null ? Math.round((model.size_vram / total) * 100) : null;
  const spilled = share != null && share < 100;
  return (
    <>
      <span className={spilled ? "warn" : "good"}>
        {share == null ? "Loaded" : spilled ? `Loaded, ${share}% on GPU` : "Loaded on GPU"}
      </span>
      <small>
        {formatNumber(model.context_length)} context
        {spilled ? ". The rest runs on the CPU and is much slower; lower num_ctx or use a smaller model." : ""}
      </small>
    </>
  );
}

function ProfileSummary({ profile }: { profile: Profile }) {
  const { num_ctx, ...rest } = profile.options as Record<string, unknown> & { num_ctx?: number };
  const parts = [
    num_ctx ? `${formatNumber(Number(num_ctx))} context` : null,
    profile.think === false ? "thinking off" : profile.think === true ? "thinking on" : null,
    ...Object.entries(rest).map(([key, value]) => `${key} ${value}`),
  ].filter(Boolean);
  return (
    <>
      <span>{parts.join(", ") || "Passed through unchanged"}</span>
      <small>From {profile.matched.join(", then ") || "no profile"}</small>
    </>
  );
}
