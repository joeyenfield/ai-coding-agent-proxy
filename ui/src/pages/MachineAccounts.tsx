import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type FleetMachine, type ProviderCli } from "../api";
import { CodeLine } from "../components/CodeLine";
import { StateDot } from "../components/ProviderState";
import { useClipboard } from "../hooks";
import { AccountStateBadge } from "./Subscriptions";

const ENROLL = "agent accounts enroll claude";

const AUTH_LABELS: Record<ProviderCli["auth"], string> = { signed_in: "Signed in", signed_out: "Signed out", unknown: "Can't tell" };

/**
 * A machine's agent accounts: the provider CLIs it found (Claude Code, Codex, Copilot CLI) and whether
 * each is signed in, as its agent-machine reports them (never a token), and the subscriptions assigned to it.
 */
export function MachineAccounts({ machine }: { machine: FleetMachine }) {
  const accounts = useQuery({ queryKey: ["provider-accounts", machine.id], queryFn: () => api.providerAccounts(machine.id), refetchInterval: 10000 });
  const reported = machine.provider_clis;
  const copy = useClipboard();
  return (
    <>
      <section className="panel" aria-labelledby="clis-title">
        <h2 id="clis-title">Provider CLIs</h2>
        <p className="subtle">
          What {machine.name} found, checked every few minutes. It looks at whether each is signed in without reading the sign-in, and sends
          the server only that.
        </p>
        {reported == null ? (
          <p className="empty">{machine.name}'s agent-machine is too old to report them: run <code>agent-machine update</code> there.</p>
        ) : reported.clis.length === 0 ? (
          <p className="empty">{reported.error ?? `${machine.name} hasn't looked yet.`}</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>CLI</th>
                <th>Version</th>
                <th>Sign-in</th>
              </tr>
            </thead>
            <tbody>
              {reported.clis.map((cli) => (
                <tr key={cli.provider}>
                  <td className="mono">{cli.provider}</td>
                  <td>{cli.installed ? cli.version ?? "Installed" : <span className="subtle">Not installed</span>}</td>
                  <td>
                    {cli.installed ? (
                      <span className="row">
                        <StateDot tone={cli.auth === "signed_in" && cli.subscription !== false ? "good" : cli.auth === "signed_out" ? "bad" : "idle"} />
                        {AUTH_LABELS[cli.auth]}
                        {cli.subscription === false && <span className="warn"> · with an API key, not a subscription</span>}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="panel" aria-labelledby="machine-accounts-title">
        <h2 id="machine-accounts-title">Subscriptions here</h2>
        {(accounts.data?.accounts ?? []).length === 0 ? (
          <div className="stack">
            <p className="subtle">
              None of {machine.owner ? `${machine.owner}'s` : "its owner's"} subscriptions you can see run here.{" "}
              {machine.you.owner && "Sign in with the provider's CLI there, then register it:"}
            </p>
            {machine.you.owner && <CodeLine text={ENROLL} onCopy={() => copy(ENROLL, "Command copied")} />}
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Subscription</th>
                <th>State</th>
                <th>Here</th>
              </tr>
            </thead>
            <tbody>
              {accounts.data!.accounts.map((account) => {
                const here = account.assignments.find((item) => item.machine_id === machine.id);
                return (
                  <tr key={account.id}>
                    <td>
                      <Link to="/subscriptions">{account.display_name}</Link> <span className="subtle">{account.provider_label}</span>
                    </td>
                    <td>
                      <AccountStateBadge account={account} />
                    </td>
                    <td className={here?.health.available ? "good" : "subtle"}>{here?.health.available ? "Ready" : here?.health.error_message ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
