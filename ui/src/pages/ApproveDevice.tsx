import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type DeviceRequest } from "../api";
import { PartTag } from "../components/PartTag";
import { useTicker } from "../live";

/** A code as people type it: letters only, upper case, ABCD-EFGH. */
function tidy(value: string) {
  const letters = value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 8);
  return letters.length > 4 ? `${letters.slice(0, 4)}-${letters.slice(4)}` : letters;
}

function ago(seconds: number | null) {
  if (seconds == null) return "earlier";
  const elapsed = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (elapsed < 60) return "just now";
  return `${Math.round(elapsed / 60)} min ago`;
}

function remaining(until: number) {
  const left = Math.max(0, Math.round(until - Date.now() / 1000));
  return left >= 60 ? `${Math.floor(left / 60)} min ${String(left % 60).padStart(2, "0")} s` : `${left} s`;
}

/**
 * Machines › Approve a device: sign in `agent login` or `agent-machine login` on another computer
 * as yourself, from the console you're already signed in to, without typing your password again.
 * The code is always typed here (never taken from a link), and what asked is shown before deciding.
 */
export function ApproveDevicePage() {
  const [code, setCode] = useState("");
  const [request, setRequest] = useState<DeviceRequest | null>(null);
  const [decided, setDecided] = useState<boolean | null>(null);
  useTicker(request != null && decided == null, 1000);
  const check = useMutation({
    mutationFn: () => api.checkDevice(code),
    onSuccess: (found) => {
      setRequest(found);
      setDecided(null);
    },
  });
  const decide = useMutation({
    mutationFn: (approve: boolean) => api.decideDevice(code, approve),
    onSuccess: (result) => setDecided(result.approved),
  });
  const startOver = () => {
    setCode("");
    setRequest(null);
    setDecided(null);
    check.reset();
    decide.reset();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (code.replace("-", "").length === 8) check.mutate();
  };
  const expired = request != null && request.expires_at <= Date.now() / 1000;
  return (
    <div className="page">
      <header className="page-head">
        <PartTag />
        <h1>Approve a device</h1>
        <p className="lede">
          Sign in <code>agent login</code> or <code>agent-machine login</code> on another computer as you, without typing your password
          there. Enter the code it shows.
        </p>
      </header>
      <section className="panel device-approve">
        {decided == null && request == null && (
          <form className="device-code-form" onSubmit={submit}>
            <label>
              Code
              <input
                className="device-code-input mono"
                value={code}
                onChange={(event) => {
                  setCode(tidy(event.target.value));
                  check.reset();
                }}
                placeholder="ABCD-EFGH"
                autoComplete="one-time-code"
                autoCapitalize="characters"
                spellCheck={false}
                autoFocus
                aria-describedby="device-code-help"
              />
            </label>
            <small id="device-code-help" className="subtle">
              Eight letters, shown by the login command along with a link to the sign-in page. It works for 10 minutes.
            </small>
            {check.isError && (
              <p className="bad" role="alert">
                {check.error.message}
              </p>
            )}
            <div className="row">
              <button type="submit" className="primary" disabled={code.replace("-", "").length !== 8 || check.isPending}>
                {check.isPending ? "Checking…" : "Continue"}
              </button>
            </div>
          </form>
        )}
        {decided == null && request != null && (
          <div className="device-request">
            <p className="device-code-shown mono">{code}</p>
            <dl className="fact-list">
              <div>
                <dt>Asked by</dt>
                <dd>{request.client}</dd>
              </div>
              <div>
                <dt>Asked</dt>
                <dd>{ago(request.requested_at)}</dd>
              </div>
              <div>
                <dt>Signs in as</dt>
                <dd>{request.user}</dd>
              </div>
              <div>
                <dt>Code expires</dt>
                <dd className={expired ? "bad" : undefined}>{expired ? "Expired" : `in ${remaining(request.expires_at)}`}</dd>
              </div>
            </dl>
            <p className="callout" role="note">
              Only approve a code from a sign-in you started yourself. If someone sent you this code, deny it: approving lets their
              computer use your account.
            </p>
            {decide.isError && (
              <p className="bad" role="alert">
                {decide.error.message}
              </p>
            )}
            <div className="row">
              <button type="button" className="primary" disabled={expired || decide.isPending} onClick={() => decide.mutate(true)}>
                Approve
              </button>
              <button type="button" disabled={expired || decide.isPending} onClick={() => decide.mutate(false)}>
                Deny
              </button>
              <button type="button" className="link" onClick={startOver}>
                Use another code
              </button>
            </div>
          </div>
        )}
        {decided != null && (
          <div className="device-result" role="status">
            <h2>{decided ? "Device approved" : "Device denied"}</h2>
            <p className="subtle">
              {decided
                ? "That computer finishes signing in by itself in a few seconds: go back to its terminal."
                : "That sign-in was refused, and its code can't be used again. Nothing was signed in."}
            </p>
            <div className="row">
              <button type="button" onClick={startOver}>
                Approve another device
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
