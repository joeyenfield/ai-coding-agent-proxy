import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useVersion } from "../hooks";

// Why the API sent a browser back here (?sign_in=, from /api/auth/login and /api/auth/callback).
const PROBLEMS: Record<string, string> = {
  failed: "Sign-in couldn't be completed, perhaps because it took too long or was started in another tab. Try again.",
  unavailable: "The sign-in service isn't answering right now. Try again in a minute.",
  too_many: "This browser is signed in to as many accounts as it can hold. Sign out of one, then add the other.",
};

/** Read and drop ?sign_in=, so a reload doesn't show the message again. */
function takeProblem(): string | null {
  const url = new URL(window.location.href);
  const reason = url.searchParams.get("sign_in");
  if (!reason) return null;
  url.searchParams.delete("sign_in");
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  return PROBLEMS[reason] ?? PROBLEMS.failed;
}

export function LoginPage() {
  const health = useVersion();
  const [problem, setProblem] = useState(takeProblem);
  // Accounts this browser is still signed in to, when the active one's sign-in has ended.
  const held = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: false });
  const continueAs = (user: string) =>
    api.switchAccount(user).then(() => window.location.reload(), () => {
      setProblem(`The sign-in for ${user} has ended. Sign in again.`);
      void held.refetch();
    });
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
        {problem && (
          <p className="bad" role="alert">
            {problem}
          </p>
        )}
        <p className="subtle">You'll sign in on the AI Control Mesh sign-in page, then come straight back here. Your password is never sent to the console or API.</p>
        {(held.data?.accounts.length ?? 0) > 0 && (
          <div className="login-accounts">
            <p className="subtle">This browser is still signed in to:</p>
            {held.data?.accounts.map((account) => (
              <button key={account.user} type="button" onClick={() => void continueAs(account.user)}>
                Continue as <strong>{account.user}</strong>
              </button>
            ))}
          </div>
        )}
        <button type="button" className="primary" onClick={() => api.login()}>
          {(held.data?.accounts.length ?? 0) > 0 ? "Sign in to another account" : "Continue to sign in"}
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
