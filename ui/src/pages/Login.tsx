import { api } from "../api";
import { useVersion } from "../hooks";

export function LoginPage() {
  const health = useVersion();
  return (
    <main className="login">
      <section className="login-card panel">
        <div className="wordmark">
          <svg viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" />
            <path d="M8 11h6l4 10h6" />
          </svg>
          <span>AI Control Mesh</span>
        </div>
        <h1>Sign in</h1>
        <p className="subtle">Sign in through the authentication service. Your password is never sent to the console or API.</p>
        <button type="button" className="primary" onClick={() => api.login()}>
          Continue to sign in
        </button>
        <p className="subtle login-downloads">
          Need the client, agent-machine or the Android app? <a href="/get">Downloads</a> and the <a href="/help">documentation</a> don't need an account.
        </p>
        {health.data?.version && (
          <p className="subtle login-version">
            Version <span className="mono">{health.data.version}</span>
          </p>
        )}
      </section>
    </main>
  );
}
