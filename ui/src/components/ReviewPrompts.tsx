import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "../api";
import { ConfirmDialog } from "./ConfirmDialog";

/**
 * Server settings, Review prompts: the AI reviews a session's Review tab offers, edited as one YAML
 * document. The server checks it (the names a prompt may list for its data and metrics are the server's)
 * and says what's wrong; nothing is parsed here.
 */
export function ReviewPromptsSettings() {
  const table = useQuery({ queryKey: ["review-prompts"], queryFn: api.reviewPrompts, retry: false });
  const yaml = useQuery({ queryKey: ["review-prompts", "yaml"], queryFn: api.reviewPromptsYaml, retry: false });
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [message, setMessage] = useState<{ text: string; tone: "good" | "bad" } | null>(null);
  const [asking, setAsking] = useState(false);
  useEffect(() => {
    if (yaml.data !== undefined) setText(yaml.data);
  }, [yaml.data]);
  const saved = (said: string) => {
    setMessage({ text: said, tone: "good" });
    client.invalidateQueries({ queryKey: ["review-prompts"] });
    client.invalidateQueries({ queryKey: ["session-review"] });
  };
  const save = useMutation({
    mutationFn: () => api.saveReviewPrompts(text),
    onSuccess: () => saved("Saved; the next review uses them."),
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  const reset = useMutation({
    mutationFn: () => api.saveReviewPrompts(null),
    onSuccess: () => saved("Back to the prompts the server ships with."),
    onError: (error) => setMessage({ text: error.message, tone: "bad" }),
  });
  // Site admins only on the server: anyone else sees nothing here rather than an error.
  if (table.error instanceof ApiError && table.error.status === 403) return null;
  return (
    <section className="section" aria-labelledby="review-prompts-heading">
      <div className="section-head">
        <h2 id="review-prompts-heading">Review prompts</h2>
        {table.data && <span className="pill">{table.data.custom ? "This server's own" : "Shipped"}</span>}
      </div>
      <p className="subtle">
        The AI reviews offered on a session's Review tab. Each prompt has an <code>id</code>, a <code>title</code> (its button), a line{" "}
        <code>about</code> it, its <code>focus</code> (what the model is asked to look at and say), the <code>data</code> it is sent and
        the <code>metrics</code> (checks) whose findings go with it. The rules every review shares, and the form of the answer, are
        added by the server. The first prompt is the one chosen to begin with.
      </p>
      {table.isError && <p className="bad">{table.error.message}</p>}
      <form
        className="stored-editor panel"
        onSubmit={(event) => {
          event.preventDefault();
          setMessage(null);
          save.mutate();
        }}
      >
        <textarea
          aria-label="Review prompts (YAML)"
          rows={Math.min(40, Math.max(12, text.split("\n").length + 1))}
          value={text}
          onChange={(event) => setText(event.target.value)}
          spellCheck={false}
        />
        <div className="row wrap">
          <button className="primary" type="submit" disabled={save.isPending || yaml.data === undefined}>
            {save.isPending ? "Saving…" : "Save prompts"}
          </button>
          {table.data?.custom && (
            <button type="button" disabled={reset.isPending} onClick={() => setAsking(true)}>
              Use the shipped prompts
            </button>
          )}
          {message && (
            <span className={message.tone} role="status">
              {message.text}
            </span>
          )}
        </div>
      </form>
      {table.data && (
        <div className="review-names">
          <details>
            <summary>Names for data: what a prompt can be sent</summary>
            <dl>
              {Object.entries(table.data.data).map(([name, label]) => (
                <div key={name}>
                  <dt>
                    <code>{name}</code>
                  </dt>
                  <dd>{label}</dd>
                </div>
              ))}
            </dl>
          </details>
          <details>
            <summary>Names for metrics: the checks</summary>
            <dl>
              {Object.entries(table.data.metrics).map(([name, label]) => (
                <div key={name}>
                  <dt>
                    <code>{name}</code>
                  </dt>
                  <dd>{label}</dd>
                </div>
              ))}
            </dl>
          </details>
        </div>
      )}
      <ConfirmDialog
        open={asking}
        title="Use the shipped prompts?"
        confirmLabel="Use the shipped prompts"
        onCancel={() => setAsking(false)}
        onConfirm={() => {
          setAsking(false);
          setMessage(null);
          reset.mutate();
        }}
      >
        <p>This server's own review prompts are removed. Reviews already made keep their answers.</p>
      </ConfirmDialog>
    </section>
  );
}
