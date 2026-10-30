import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Identity, type SessionYou, type Sharing, type Visibility } from "../api";
import { useAuth, useToast } from "../hooks";

/** "Only you", "Team acme/core", "Everyone in acme", plus whether they can type. */
export function sharingText(you: SessionYou, short = false): string {
  const { visibility, team, org, owner, access } = you.sharing;
  // Sessions shared with "org" before there were several organizations have none: everyone signed in sees them.
  const everyone = org ? (short ? org : `Everyone in ${org}`) : short ? "Everyone" : "Everyone signed in";
  const who = visibility === "private" ? (you.owner ? "Only you" : `Only ${owner}`) : visibility === "team" ? `Team ${team}` : everyone;
  if (visibility === "private") return who;
  return `${who}, ${access === "control" ? "can type" : "watch only"}`;
}

/** A small label for lists: who started it and who else can see it. */
export function SharingBadge({ you }: { you?: SessionYou }) {
  if (!you) return null;
  return (
    <span className={`pill sharing sharing-${you.sharing.visibility}`} title={you.owner ? "Yours" : `Started by ${you.sharing.owner}`}>
      {you.owner ? "" : `${you.sharing.owner} · `}
      {sharingText(you, true)}
    </span>
  );
}

/** Who can see a session; its owner can change that here. */
export function SharingControl({ sessionId, you }: { sessionId: string; you?: SessionYou }) {
  const auth = useAuth();
  const identity = auth.data?.identity;
  const client = useQueryClient();
  const notify = useToast();
  // Shown at once, then confirmed by the next refresh (or put back if the change fails).
  const [pending, setPending] = useState<Pick<Sharing, "visibility" | "team" | "org" | "access"> | null>(null);
  const share = useMutation({
    mutationFn: (sharing: Pick<Sharing, "visibility" | "team" | "org" | "access">) => api.shareSession(sessionId, sharing),
    onMutate: (sharing) => setPending(sharing),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["sessions"] }),
        client.invalidateQueries({ queryKey: ["terminals"] }),
        client.invalidateQueries({ queryKey: ["session", sessionId] }),
      ]);
      setPending(null);
    },
    onError: (error) => {
      setPending(null);
      notify(error.message);
    },
  });
  if (!you || !identity) return null;
  if (pending) you = { ...you, sharing: { ...you.sharing, ...pending } };
  if (!you.owner) {
    return (
      <p className="subtle sharing-note">
        Started by <strong>{you.sharing.owner ?? "someone before accounts"}</strong>, shared with {sharingText(you).replace(/,.*/, "").replace(/^Only /, "only ")}.{" "}
        {you.control ? "You can watch it, type into its terminal and end it." : "You can watch it."}
      </p>
    );
  }
  const { visibility: shared, team: sharedTeam, org: sharedOrg } = you.sharing;
  const current = shared === "team" ? `team:${sharedTeam}` : shared === "org" && sharedOrg ? `org:${sharedOrg}` : shared;
  const change = (value: string, access = you.sharing.access) => {
    const [visibility, target] = value.split(":") as [Visibility, string | undefined];
    share.mutate({ visibility, team: visibility === "team" ? target ?? null : null, org: visibility === "org" ? target ?? null : null, access });
  };
  return (
    <div className="sharing-control">
      <label className="field">
        Who can see it
        <select value={current} disabled={share.isPending} onChange={(event) => change(event.target.value)}>
          <ShareOptions identity={identity} />
          {current === "org" && <option value="org">Everyone signed in (shared before organizations)</option>}
        </select>
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={you.sharing.access === "control"}
          disabled={share.isPending || you.sharing.visibility === "private"}
          onChange={(event) => change(current, event.target.checked ? "control" : "view")}
        />
        They can type into its terminal and end it
      </label>
      {identity.orgs.length === 0 && <small className="subtle">You aren't in an organization yet; an admin adds you on Organizations.</small>}
    </div>
  );
}

/** The launcher flags for sharing a new session, for launch commands. */
export function shareArgs(identity: Identity | null | undefined, value: string, control: boolean): string[] {
  if (!identity || value === "private") return [];
  const [visibility, target] = value.split(":");
  // The launcher names the team, or the organization with --share org, in --team; with only one there's no need.
  const choices = visibility === "team" ? identity.teams : identity.orgs;
  return ["--share", visibility, ...(target && choices.length > 1 ? ["--team", target] : []), ...(control ? ["--let-others-type"] : [])];
}

/** The choices of who sees a session: only its owner, one of their teams, or one of their organizations. */
function ShareOptions({ identity }: { identity: Identity }) {
  return (
    <>
      <option value="private">Only me</option>
      {identity.teams.map((team) => (
        <option key={team} value={`team:${team}`}>
          Team {team}
        </option>
      ))}
      {identity.orgs.map((org) => (
        <option key={org} value={`org:${org}`}>
          Everyone in {org}
        </option>
      ))}
    </>
  );
}

/** Choose who sees a session you're about to start. */
export function ShareChoice({ value, control, onChange }: { value: string; control: boolean; onChange: (value: string, control: boolean) => void }) {
  const identity = useAuth().data?.identity;
  if (!identity) return null;
  return (
    <div className="sharing-control">
      <label className="field">
        Who can see this session
        <select value={value} onChange={(event) => onChange(event.target.value, control)}>
          <ShareOptions identity={identity} />
        </select>
      </label>
      <label className="check">
        <input type="checkbox" checked={control} disabled={value === "private"} onChange={(event) => onChange(value, event.target.checked)} />
        They can type into its terminal, not only watch
      </label>
    </div>
  );
}
