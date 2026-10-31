import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, roleLabel, type Identity } from "../api";
import { Icon } from "./Icon";

// Tells this browser's other console tabs that the signed-in account changed, so they start over as it.
const CHANNEL = "mesh-account";

/** Start over as whoever is signed in now: every query, live stream and terminal belongs to the old account. */
function startOver(): void {
  try {
    new BroadcastChannel(CHANNEL).postMessage("changed");
  } catch {
    // No BroadcastChannel: other tabs catch up on their next sign-in check.
  }
  window.location.reload();
}

/** Other tabs switched or signed out: follow them. */
export function useAccountChanges(): void {
  useEffect(() => {
    if (typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = () => window.location.reload();
    return () => channel.close();
  }, []);
}

/** Who's signed in, in the rail: the accounts this browser holds, to switch between, add to or sign out of. */
export function AccountMenu({ identity }: { identity: Identity }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, enabled: open });

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !box.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const act = (change: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    change()
      .then(startOver)
      .catch((problem: unknown) => {
        setBusy(false);
        setError(problem instanceof Error ? problem.message : "That didn't work. Try again.");
        void accounts.refetch();
      });
  };

  const others = (accounts.data?.accounts ?? []).filter((account) => account.user !== identity.user);
  const full = accounts.data ? accounts.data.accounts.length >= accounts.data.max : false;

  return (
    <div className="account-menu" ref={box}>
      <button
        type="button" className="signed-in account-button" aria-haspopup="menu" aria-expanded={open}
        title="Switch account or sign out" onClick={() => setOpen((value) => !value)}
      >
        <span className="avatar" aria-hidden="true">{identity.user.slice(0, 1).toUpperCase()}</span>
        <span className="signed-in-name">
          {identity.user}
          <small>{identity.site_admin ? roleLabel(identity) : `${roleLabel(identity)} · ${identity.tenant}`}</small>
        </span>
        <Icon name="chevron" size={14} />
      </button>
      {open && (
        <div className="account-popover panel" role="menu" aria-label="Accounts">
          <p className="subtle account-heading">Signed in as <strong>{identity.user}</strong></p>
          {others.length > 0 && (
            <ul className="account-list">
              {others.map((account) => (
                <li key={account.user}>
                  <button type="button" role="menuitem" className="account-switch" disabled={busy} onClick={() => act(() => api.switchAccount(account.user))}>
                    <span className="avatar" aria-hidden="true">{account.user.slice(0, 1).toUpperCase()}</span>
                    <span>Switch to <strong>{account.user}</strong></span>
                  </button>
                  <button
                    type="button" className="icon-button" title={`Sign out of ${account.user}`} aria-label={`Sign out of ${account.user}`}
                    disabled={busy} onClick={() => act(() => api.logout({ user: account.user }))}
                  >
                    <Icon name="logout" size={16} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {error && <p className="bad" role="alert">{error}</p>}
          <div className="account-actions">
            <button type="button" role="menuitem" disabled={busy || full} onClick={() => api.addAccount()} title={full ? "This browser holds as many sign-ins as it can; sign out of one first" : undefined}>
              <Icon name="plus" size={16} /> Add another account
            </button>
            <button type="button" role="menuitem" disabled={busy} onClick={() => act(() => api.logout())}>
              <Icon name="logout" size={16} /> Sign out of {identity.user}
            </button>
            {others.length > 0 && (
              <button type="button" role="menuitem" className="danger" disabled={busy} onClick={() => act(() => api.logout({ all: true }))}>
                Sign out of every account
              </button>
            )}
          </div>
          <p className="subtle account-note">Each account belongs to one organization, and sees only what's in it.</p>
        </div>
      )}
    </div>
  );
}
