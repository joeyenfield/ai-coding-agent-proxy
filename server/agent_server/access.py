"""Who can see and control a session, once the proxy has accounts.

Whoever starts a session owns it and decides who else gets it:

  visibility  private   only the owner (admins included: private means private)
              team      members of the team the owner picked
              org       everyone in the organization
  access      view      others can read its traffic and watch its terminal
              control   others can also type into its terminal and end it

The sharing lives in the session's tags (owner, visibility, team, access), which
only these functions write. Sessions from before accounts existed have no owner;
admins can see those. Without accounts, everyone can see and control everything.
"""

from __future__ import annotations

from typing import Any

from .auth import Identity
from .sessions import Session


VISIBILITY = ("private", "team", "org")
ACCESS = ("view", "control")
SHARING_TAGS = ("owner", "visibility", "team", "access")


def sharing(session: Session) -> dict[str, Any]:
    tags = session.tags
    return {
        "owner": tags.get("owner"), "visibility": tags.get("visibility", "private"),
        "team": tags.get("team"), "access": tags.get("access", "view"),
    }


def can_view(identity: Identity | None, session: Session) -> bool:
    if identity is None:
        return True
    shared = sharing(session)
    if shared["owner"] is None:
        return identity.admin
    if shared["owner"] == identity.user:
        return True
    if shared["visibility"] == "org":
        return True
    return shared["visibility"] == "team" and shared["team"] in identity.teams


def can_control(identity: Identity | None, session: Session) -> bool:
    """Type into its terminal, end it, change capture, clear or delete it."""
    if identity is None:
        return True
    shared = sharing(session)
    if shared["owner"] is None:
        return identity.admin
    return shared["owner"] == identity.user or (can_view(identity, session) and shared["access"] == "control")


def is_owner(identity: Identity | None, session: Session) -> bool:
    return identity is None or session.tags.get("owner") == identity.user


def sharing_tags(identity: Identity, requested: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated sharing for a session this identity owns. Raises ValueError."""
    current = current or {}
    visibility = str(requested.get("visibility") or current.get("visibility") or "private")
    if visibility not in VISIBILITY:
        raise ValueError(f"Visibility must be one of: {', '.join(VISIBILITY)}")
    access = str(requested.get("access") or current.get("access") or "view")
    if access not in ACCESS:
        raise ValueError(f"Access must be one of: {', '.join(ACCESS)}")
    team = requested.get("team") if "team" in requested else current.get("team")
    if visibility == "team":
        team = team or (identity.teams[0] if len(identity.teams) == 1 else None)
        if not team:
            raise ValueError("Pick which of your teams can see it" if identity.teams else "You aren't in a team yet; ask an admin to add you to one")
        if team not in identity.teams:
            raise ValueError(f"You aren't in the {team} team")
    else:
        team = None
    return {"owner": identity.user, "visibility": visibility, "team": team, "access": access}


def client_tags(tags: Any) -> dict[str, Any]:
    """Tags a client sent, without the sharing ones only the proxy sets."""
    return {key: value for key, value in (tags or {}).items() if key not in SHARING_TAGS} if isinstance(tags, dict) else {}


def viewer(identity: Identity | None, session: Session) -> dict[str, Any]:
    """What the current user may do with a session, for the console."""
    return {
        "owner": is_owner(identity, session), "control": can_control(identity, session),
        "sharing": sharing(session),
    }
