"""The browser's address, as the API passes it to agent-auth when it calls /token for the console.

The console never calls the issuer itself: the API renews and switches its sign-ins with the
refresh tokens kept in its cookie. To agent-auth all those calls come from the API's one
address (through a CDN, the address of the server's whole network), so its per-address limit
on /token would be shared by every browser, and by every agent on that network too. The API
names the browser's address instead, with an HMAC under the introspection secret, which only
the two processes hold; agent-auth counts the call under that address when the proof matches,
and under the connection's own address otherwise.
"""

from __future__ import annotations

import hashlib
import hmac

ADDRESS_HEADER = "X-Mesh-Relayed-For"
PROOF_HEADER = "X-Mesh-Relay-Proof"


def proof(secret: str, address: str) -> str:
    return hmac.new(secret.encode(), b"token-relay\0" + address.encode(), hashlib.sha256).hexdigest()


def headers(secret: str | None, address: str | None) -> dict[str, str]:
    """The headers naming address, or none without a secret or an address."""
    if not secret or not address:
        return {}
    return {ADDRESS_HEADER: address, PROOF_HEADER: proof(secret, address)}


def relayed_for(secret: str | None, address: str | None, given: str | None) -> str | None:
    """The address a relayed call names, if its proof is right; None otherwise."""
    if not secret or not address or not given or len(address) > 100:
        return None
    return address if hmac.compare_digest(proof(secret, address), given) else None
