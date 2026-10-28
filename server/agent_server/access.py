"""Who can see and control a session, once the proxy has accounts.

Whoever starts a session owns it and decides who else gets it:

  visibility  private   only the owner (admins included: private means private)
              team      members of the team the owner picked (<org>/<team>)
              org       members of the organization the owner picked
  access      view      others can read its traffic and watch its terminal
              control   others can also type into its terminal and end it

The sharing lives in the session's tags (owner, visibility, team, org, access),
which only these functions write. Sessions from before accounts existed have no
owner; admins can see those. Sessions shared before organizations name a team
without its organization (matched by the team's own name) or "org" without one
(everyone signed in), as the server was then a single organization. Without
accounts, everyone can see and control everything.

Launchers name the organization in `team` (`agent --share org --team acme`), so
`org` falls back to it.
"""

from __future__ import annotations

from typing import Any

from .auth import Identity, team_org
from .sessions import Session


VISIBILITY = ("private", "team", "org")
ACCESS = ("view", "control")
SHARING_TAGS = ("owner", "visibility", "team", "org", "access")
# Set by the server (routes/machines.py) on a launch or terminal someone a machine lets in started there:
# the session is the machine's, its owner's, so this is how they get what they started. Clients can't set it.
SERVER_TAGS = (*SHARING_TAGS, "launched_by")


def sharing(session: Session) -> dict[str, Any]:
    tags = session.tags
    return {
        "owner": tags.get("owner"), "visibility": tags.get("visibility", "private"),
        "team": tags.get("team"), "org": tags.get("org"), "access": tags.get("access", "view"),
    }


def _in_team(identity: Identity, team: str | None) -> bool:
    if not team:
        return False
    if "/" in team:
        return team in identity.teams
    return any(mine.split("/", 1)[1] == team for mine in identity.teams)


def can_view(identity: Identity | None, session: Session) -> bool:
    if identity is None:
        return True
    shared = sharing(session)
    if shared["owner"] is None:
        return identity.admin
    if shared["owner"] == identity.user or session.tags.get("launched_by") == identity.user:
        return True
    if shared["visibility"] == "org":
        return shared["org"] is None or shared["org"] in identity.orgs
    return shared["visibility"] == "team" and _in_team(identity, shared["team"])


def can_control(identity: Identity | None, session: Session) -> bool:
    """Type into its terminal, end it, change capture, clear or delete it."""
    if identity is None:
        return True
    shared = sharing(session)
    if shared["owner"] is None:
        return identity.admin
    if session.tags.get("launched_by") == identity.user:
        return True
    return shared["owner"] == identity.user or (can_view(identity, session) and shared["access"] == "control")


def is_owner(identity: Identity | None, session: Session) -> bool:
    return identity is None or session.tags.get("owner") == identity.user


def _pick(value: Any, mine: tuple[str, ...], what: str) -> str:
    """One of the user's teams or organizations: the one named, or their only one."""
    value = str(value or "").strip()
    if not value:
        if len(mine) == 1:
            return mine[0]
        raise ValueError(f"Pick which of your {what}s can see it" if mine else f"You aren't in {'an' if what[0] in 'aeiou' else 'a'} {what} yet; ask an admin to add you to one")
    if value in mine:
        return value
    # A team named without its organization, as launchers did before organizations.
    matches = [item for item in mine if what == "team" and item.split("/", 1)[1] == value]
    if len(matches) == 1:
        return matches[0]
    raise ValueError(f"You aren't in the {value} {what}" + (f"; name it as <organization>/{value}" if len(matches) > 1 else ""))


def sharing_tags(identity: Identity, requested: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated sharing for a session this identity owns. Raises ValueError."""
    current = current or {}
    visibility = str(requested.get("visibility") or current.get("visibility") or "private")
    if visibility not in VISIBILITY:
        raise ValueError(f"Visibility must be one of: {', '.join(VISIBILITY)}")
    access = str(requested.get("access") or current.get("access") or "view")
    if access not in ACCESS:
        raise ValueError(f"Access must be one of: {', '.join(ACCESS)}")
    team = org = None
    if visibility == "team":
        team = _pick(requested.get("team") if "team" in requested else current.get("team"), identity.teams, "team")
        org = team_org(team)
    elif visibility == "org":
        named = requested.get("org") or requested.get("team") if "org" in requested or "team" in requested else current.get("org")
        org = _pick(named, identity.orgs, "organization")
    return {"owner": identity.user, "visibility": visibility, "team": team, "org": org, "access": access}


def client_tags(tags: Any) -> dict[str, Any]:
    """Tags a client sent, without the sharing ones only the proxy sets."""
    return {key: value for key, value in (tags or {}).items() if key not in SERVER_TAGS} if isinstance(tags, dict) else {}


def viewer(identity: Identity | None, session: Session) -> dict[str, Any]:
    """What the current user may do with a session, for the console."""
    return {
        "owner": is_owner(identity, session), "control": can_control(identity, session),
        "sharing": sharing(session),
    }
