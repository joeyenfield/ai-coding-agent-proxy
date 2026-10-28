import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type FleetMachine, type MachineAccess as Grants, type MachineFeature } from "../api";
import { Icon } from "../components/Icon";
import { useAuth, useMachines, useToast } from "../hooks";

/** The features in the order the server lists them, with a fallback for an older server. */
const FALLBACK: Record<MachineFeature, string> = {
  view: "See it: its model servers, their models and logs, its load and the agents running there",
  launch: "Start agents there, and browse its launch folders",
  terminal: "Open terminals there: a shell as the user agent-machine runs as",
  models: "Pull, delete, load and unload models on its model servers",
  servers: "Start, stop and configure the Ollama and Strata installed there",
  update: "Update agent-machine and agent there to this server's build",
  mcp: "Use the MCP servers it shares in the agents they start (they run there, as its user)",
};
const LABELS: Record<MachineFeature, string> = {
  view: "See it",
  launch: "Start agents",
  terminal: "Open terminals",
  models: "Change models",
  servers: "Run model servers",
  update: "Update it",
  mcp: "Use MCP servers",
};

/** The switch on the machine itself that a feature also needs, if any, and whether it's on. */
function machineSwitch(machine: FleetMachine, feature: MachineFeature): { on: boolean; command: string } | null {
  switch (feature) {
    case "launch":
      return { on: machine.launches, command: "agent-machine launches on" };
    case "terminal":
      return { on: Boolean(machine.commands), command: "agent-machine commands on" };
    case "models":
    case "servers":
      return { on: Boolean(machine.servers.enabled), command: "agent-machine servers on" };
    case "update":
      return { on: Boolean(machine.updates), command: "agent-machine updates on" };
    default:
      return null;
  }
}

/**
 * Who besides its owner may use each of a machine's features: organizations and teams, picked by its
 * owner (from their own) or an admin (from any). The machine's own switches still apply on top.
 */
export function MachineAccess({ machine }: { machine: FleetMachine }) {
  const auth = useAuth();
  const machines = useMachines();
  const client = useQueryClient();
  const notify = useToast();
  const features = (machines.data?.features ?? FALLBACK) as Record<MachineFeature, string>;
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs, enabled: Boolean(auth.data?.required), retry: false });
  const [draft, setDraft] = useState<Grants>(machine.access ?? {});
  // A save elsewhere (or the machine's next update) brings new grants; an unsaved draft stays.
  const saved = JSON.stringify(machine.access ?? {});
  const [base, setBase] = useState(saved);
  useEffect(() => {
    if (saved !== base) {
      setDraft(JSON.parse(saved) as Grants);
      setBase(saved);
    }
  }, [saved, base]);
  const dirty = JSON.stringify(normal(draft)) !== JSON.stringify(normal(machine.access ?? {}));
  const save = useMutation({
    mutationFn: () => api.setMachineAccess(machine.id, normal(draft)),
    onSuccess: () => {
      notify(`Saved who can use ${machine.name}.`);
      client.invalidateQueries({ queryKey: ["machines"] });
    },
    onError: (error: Error) => notify(error.message),
  });
  // What can be granted: organizations and their teams, as the server lists them for this user.
  const choices = useMemo(() => {
    const list: { kind: "orgs" | "teams"; value: string; label: string }[] = [];
    for (const org of orgs.data?.orgs ?? []) {
      list.push({ kind: "orgs", value: org.id, label: `${org.name} (organization)` });
      for (const team of org.teams) list.push({ kind: "teams", value: team.key, label: `${org.name} › ${team.name} (team)` });
    }
    return list;
  }, [orgs.data]);
  const labelOf = (kind: "orgs" | "teams", value: string) => choices.find((item) => item.kind === kind && item.value === value)?.label ?? value;

  if (!auth.data?.required) {
    return (
      <section className="section">
        <div className="section-head">
          <h2>Access</h2>
        </div>
        <p className="empty">
          This server has no accounts, so everyone who can open this console can use every feature of {machine.name}. Once
          you add users (Organizations), you can pick which organizations and teams may use each feature here.
        </p>
      </section>
    );
  }
  if (!machine.you.manage) {
    return (
      <section className="section">
        <div className="section-head">
          <h2>Access</h2>
        </div>
        <p className="empty">Only {machine.owner ?? "its owner"} or an admin can change who can use {machine.name}.</p>
      </section>
    );
  }

  const change = (feature: MachineFeature, kind: "orgs" | "teams", value: string, add: boolean) => {
    setDraft((current) => {
      const grant = { orgs: [...(current[feature]?.orgs ?? [])], teams: [...(current[feature]?.teams ?? [])] };
      grant[kind] = add ? [...new Set([...grant[kind], value])] : grant[kind].filter((item) => item !== value);
      return { ...current, [feature]: grant };
    });
  };

  return (
    <section className="section machine-access" aria-labelledby="machine-access">
      <div className="section-head">
        <h2 id="machine-access">Access</h2>
        <p className="subtle">
          Which organizations and teams may use each feature of {machine.name}. {machine.owner ?? "Its owner"} can use them all;
          admins can see it, change its models and update it. Anyone given a feature can also see the machine. Agents and
          terminals they start run there as {machine.owner ?? "its owner"}'s sign-in, and they can watch and use what they start.
        </p>
      </div>
      {orgs.isError && <p className="bad">{orgs.error.message}</p>}
      <ul className="access-list">
        {(Object.keys(features) as MachineFeature[]).map((feature) => {
          const grant = draft[feature] ?? { orgs: [], teams: [] };
          const toggle = machineSwitch(machine, feature);
          const left = choices.filter((item) => !grant[item.kind].includes(item.value));
          return (
            <li key={feature}>
              <div className="access-feature">
                <strong>{LABELS[feature] ?? feature}</strong>
                <span className="subtle">{features[feature]}</span>
                {toggle && !toggle.on && (
                  <span className="warn">
                    Off on {machine.name} itself: run <code>{toggle.command}</code> there for anyone to use it.
                  </span>
                )}
                {feature === "terminal" && (grant.orgs.length > 0 || grant.teams.length > 0) && (
                  <span className="warn">A terminal is a shell on {machine.name}: whoever you add can run any command there as its user.</span>
                )}
              </div>
              <div className="access-grants">
                {grant.orgs.length + grant.teams.length === 0 && <span className="subtle">Only {machine.owner ?? "its owner"}{feature === "view" || feature === "models" || feature === "update" ? " and admins" : ""}</span>}
                {(["orgs", "teams"] as const).flatMap((kind) =>
                  grant[kind].map((value) => (
                    <span key={`${kind}:${value}`} className="pill access-chip">
                      {labelOf(kind, value)}
                      <button type="button" className="icon-button small" aria-label={`Remove ${labelOf(kind, value)}`} onClick={() => change(feature, kind, value, false)}>
                        <Icon name="close" size={12} />
                      </button>
                    </span>
                  )),
                )}
                {left.length > 0 && (
                  <select
                    aria-label={`Let another organization or team ${LABELS[feature]?.toLowerCase() ?? feature}`}
                    value=""
                    onChange={(event) => {
                      const [kind, value] = event.target.value.split("\u0000") as ["orgs" | "teams", string];
                      if (value) change(feature, kind, value, true);
                    }}
                  >
                    <option value="">Add…</option>
                    {left.map((item) => (
                      <option key={`${item.kind}:${item.value}`} value={`${item.kind}\u0000${item.value}`}>
                        {item.label}
                      </option>
                    ))}
                  </select>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {choices.length === 0 && orgs.isSuccess && <p className="subtle">You aren't in an organization yet, so there's no one to let in. An admin can add you to one.</p>}
      <div className="row wrap">
        <button type="button" className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "Saving…" : "Save"}
        </button>
        <button type="button" disabled={!dirty || save.isPending} onClick={() => setDraft(JSON.parse(saved) as Grants)}>
          Undo changes
        </button>
      </div>
    </section>
  );
}

/** The grants without empty ones, sorted, so a draft compares equal to what the server keeps. */
function normal(access: Grants): Grants {
  const out: Grants = {};
  for (const [feature, grant] of Object.entries(access) as [MachineFeature, { orgs: string[]; teams: string[] }][]) {
    const orgs = [...new Set(grant?.orgs ?? [])].sort();
    const teams = [...new Set(grant?.teams ?? [])].sort();
    if (orgs.length || teams.length) out[feature] = { orgs, teams };
  }
  return out;
}
