import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { PairPanel } from "./Terminals";

/** Where the pairing QR code leads: /pair#<key>. Pairs this browser, then opens Running agents. */
export function PairPage() {
  const navigate = useNavigate();
  const started = useRef(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const key = decodeURIComponent(window.location.hash.slice(1));
    // Keep the key out of the address bar and the browser history.
    window.history.replaceState(null, "", "/pair");
    if (!key) {
      setError("This link has no access key in it.");
      return;
    }
    api.pair(key).then(
      () => navigate("/terminals", { replace: true }),
      (failure: Error) => setError(failure.message),
    );
  }, [navigate]);
  return (
    <div className="page">
      <header className="page-head">
        <h1>Pairing this device</h1>
        {!error && <p className="lede">One moment…</p>}
      </header>
      {error && (
        <>
          <p className="bad">{error}</p>
          <PairPanel onPaired={() => navigate("/terminals", { replace: true })} />
        </>
      )}
    </div>
  );
}
