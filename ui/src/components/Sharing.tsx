import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Identity, type SessionYou, type Sharing, type Visibility } from "../api";
import { useAuth, useToast } from "../hooks";

/** "Only you", "Team core", "Everyone in the organization", plus whether they can type. */
export function sharingText(you: SessionYou, short = false): string {
  const { visibility, team, owner, access } = you.sharing;
  const who = visibility === "private" ? (you.owner ? "Only you" : `Only ${owner}`) : visibility === "team" ? `Team ${team}` : short ? "Organization" : "Everyone in the organization";
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

/** Who can see a session; its owner can change that here. Nothing shows without accounts. */
export function SharingControl({ sessionId, you }: { sessionId: string; you?: SessionYou }) {
  const auth = useAuth();
  const identity = auth.data?.identity;
  const client = useQueryClient();
  const notify = useToast();
  // Shown at once, then confirmed by the next refresh (or put back if the change fails).
  const [pending, setPending] = useState<Pick<Sharing, "visibility" | "team" | "access"> | null>(null);
  const share = useMutation({
    mutationFn: (sharing: Pick<Sharing, "visibility" | "team" | "access">) => api.shareSession(sessionId, sharing),
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
  const current = you.sharing.visibility === "team" ? `team:${you.sharing.team}` : you.sharing.visibility;
  const change = (value: string, access = you.sharing.access) => {
    const [visibility, team] = value.split(":") as [Visibility, string | undefined];
    share.mutate({ visibility, team: team ?? null, access });
  };
  return (
    <div className="sharing-control">
      <label className="field">
        Who can see it
        <select value={current} disabled={share.isPending} onChange={(event) => change(event.target.value)}>
          <option value="private">Only me</option>
          {identity.teams.map((team) => (
            <option key={team} value={`team:${team}`}>
              Team {team}
            </option>
          ))}
          <option value="org">Everyone in the organization</option>
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
      {identity.teams.length === 0 && <small className="subtle">You aren't in a team yet; an admin adds you on Organization.</small>}
    </div>
  );
}

/** The launcher flags for sharing a new session, for launch commands. */
export function shareArgs(identity: Identity | null | undefined, value: string, control: boolean): string[] {
  if (!identity || value === "private") return [];
  const [visibility, team] = value.split(":");
  return ["--share", visibility, ...(team && identity.teams.length > 1 ? ["--team", team] : []), ...(control ? ["--let-others-type"] : [])];
}

/** Choose who sees a session you're about to start; hidden without accounts. */
export function ShareChoice({ value, control, onChange }: { value: string; control: boolean; onChange: (value: string, control: boolean) => void }) {
  const identity = useAuth().data?.identity;
  if (!identity) return null;
  return (
    <div className="sharing-control">
      <label className="field">
        Who can see this session
        <select value={value} onChange={(event) => onChange(event.target.value, control)}>
          <option value="private">Only me</option>
          {identity.teams.map((team) => (
            <option key={team} value={`team:${team}`}>
              Team {team}
            </option>
          ))}
          <option value="org">Everyone in the organization</option>
        </select>
      </label>
      <label className="check">
        <input type="checkbox" checked={control} disabled={value === "private"} onChange={(event) => onChange(value, event.target.checked)} />
        They can type into its terminal, not only watch
      </label>
    </div>
  );
}
