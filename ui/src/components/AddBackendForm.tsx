import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { addBackend, freeBackendName } from "../api";
import { useToast } from "../hooks";

interface FormProps {
  type: string;
  url?: string;
  placeholder: string;
  /** Backend names already in use, so the suggested name is free. */
  taken: string[];
  onAdded?: (name: string) => void;
}

/** The name and URL of one new backend of a type, added to config/backends.yaml. */
export function BackendForm({ type, url: initialUrl = "", placeholder, taken, onAdded }: FormProps) {
  const client = useQueryClient();
  const notify = useToast();
  const [name, setName] = useState("");
  const [url, setUrl] = useState(initialUrl);
  const suggested = freeBackendName(type, taken);
  const add = useMutation({
    mutationFn: () => addBackend(type, url, name.trim() || suggested),
    onSuccess: (added) => {
      notify(`Added ${added}.`);
      setName("");
      setUrl(initialUrl);
      client.invalidateQueries();
      onAdded?.(added);
    },
    onError: (error: Error) => notify(error.message),
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    add.mutate();
  };
  return (
    <form className="add-backend-form" onSubmit={submit}>
      <label className="field">
        Name
        <input value={name} placeholder={suggested} pattern="[A-Za-z0-9][A-Za-z0-9_\-]*" onChange={(event) => setName(event.target.value)} />
      </label>
      <label className="field">
        URL
        <input type="url" required value={url} placeholder={placeholder} onChange={(event) => setUrl(event.target.value)} />
      </label>
      <button type="submit" className="primary" disabled={add.isPending || !url.trim()}>
        {add.isPending ? "Adding…" : "Add"}
      </button>
    </form>
  );
}

/** A folded BackendForm, for adding another backend from a provider's own page. */
export function AddBackendForm({ summary, ...props }: FormProps & { summary: string }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="add-backend" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{summary}</summary>
      <BackendForm {...props} onAdded={() => setOpen(false)} />
    </details>
  );
}
