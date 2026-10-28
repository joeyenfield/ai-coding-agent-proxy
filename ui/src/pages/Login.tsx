import { useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useVersion } from "../hooks";

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** The whole console, until you sign in, when the proxy has accounts. */
export function LoginPage({ secure }: { secure: boolean }) {
  const client = useQueryClient();
  const health = useVersion();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const exposed = !secure && !LOOPBACK.has(window.location.hostname);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username.trim(), password);
      setPassword("");
      await client.invalidateQueries();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="login">
      <form className="login-card panel" onSubmit={submit}>
        <div className="wordmark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" />
            <path d="M8 11h6l4 10h6" />
          </svg>
          <span>AI Proxy</span>
        </div>
        <h1>Sign in</h1>
        <p className="subtle">This proxy is shared, so you need an account. Ask an admin of your organization for one.</p>
        <label className="field">
          Username
          <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} required autoFocus />
        </label>
        <label className="field">
          Password
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required />
        </label>
        <button type="submit" className="primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {error && (
          <p className="bad" role="alert">
            {error}
          </p>
        )}
        <p className="subtle login-downloads">
          Need the client, agent-machine or the Android app? <a href="/get">Downloads</a> don't need an account.
        </p>
        {health.data?.version && (
          <p className="subtle login-version">
            Version <span className="mono">{health.data.version}</span>
          </p>
        )}
        {exposed && (
          <p className="warn">
            This connection isn't encrypted, so your password crosses the network in the clear. Use HTTPS, or a VPN such
            as Tailscale.
          </p>
        )}
      </form>
    </main>
  );
}
