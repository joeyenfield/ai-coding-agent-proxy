import { Link } from "react-router-dom";
import { useStatus } from "../hooks";

/** Ollama pages only cover local models; say where hosted and account traffic shows up instead. */
export function OllamaOnlyNote() {
  const status = useStatus();
  const hosted = Object.entries(status.data?.backends ?? {}).filter(([, backend]) => backend.type !== "ollama");
  const accounts = Boolean(status.data?.intercept?.running);
  if (!hosted.length && !accounts) return null;
  return (
    <p className="subtle route-note">
      This page covers <span className="route-badge route-ollama">Local models</span> only.{" "}
      {hosted.length > 0 && (
        <>
          <span className="route-badge route-hosted">Hosted API</span> backends ({hosted.map(([name]) => name).join(", ")}) don't report models or
          machine load.{" "}
        </>
      )}
      {accounts && (
        <>
          <span className="route-badge route-account">Account</span> agents pick their models in their own client.{" "}
        </>
      )}
      Their traffic is on the <Link to="/traffic">Traffic</Link> and <Link to="/requests">Requests</Link> pages.
    </p>
  );
}
