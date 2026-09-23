"""Our own OAuth authorization server, brokering to Entra behind it.

Why this exists. Claude Desktop can connect to a server three ways: with a
client ID you type in, by registering itself (DCR, RFC 7591), or through
Anthropic's hosted client metadata. Entra supports none of the automatic
ones - it has no dynamic registration - so pointing Claude straight at
Entra means every person who connects must first be handed a client ID.
For a client we want the opposite: paste the URL, sign in, done.

So this server becomes the authorization server Claude talks to, and Entra
stays the one that actually authenticates the person:

    Claude  --register-->  us          (we invent a client for it)
    Claude  --authorize->  us  ---->   Entra   (the sign-in the user sees)
    Entra   --callback-->  us          (we swap the code for the real token)
    Claude  --token----->  us          (we hand back Entra's token)

The token we issue is Entra's own access token, unchanged. That is the whole
trick: nothing downstream has to know this bridge exists. `entra_auth`
validates it exactly as before, and the on-behalf-of exchange still works,
because the token is genuinely Entra's and genuinely the user's.

What is deliberately NOT here: we mint no tokens of our own, keep no user
database, and store no long-lived secret belonging to anyone. The only state
is a dictionary of authorization codes that live for about a minute between
two HTTP requests.
"""

import os
import sys
import time
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

import entra_auth

# Where Entra sends the browser back to. Must be registered on the app as a
# *Web* redirect URI, because we redeem the code with a client secret.
CALLBACK_PATH = "/oauth/callback"

# Claude's own callbacks. Used only to rebuild a client record after a
# restart - see get_client.
KNOWN_CLIENT_REDIRECTS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
]

# offline_access is what makes Entra return a refresh token, which is what
# spares the user a fresh sign-in every hour.
UPSTREAM_SCOPES = "openid profile offline_access"


def public_url() -> str:
    return os.getenv("PUBLIC_URL", "").rstrip("/")


def callback_url() -> str:
    return f"{public_url()}{CALLBACK_PATH}"


def configured() -> bool:
    """The bridge needs a secret; without one we cannot redeem Entra's code."""
    return bool(entra_auth.configured() and public_url())


class EntraOAuthBridge(OAuthAuthorizationServerProvider):
    """An authorization server whose only job is to stand in front of Entra."""

    def __init__(self) -> None:
        # client_id -> registration. Lost on restart; see get_client.
        self._clients: dict[str, OAuthClientInformationFull] = {}
        # Our authorization code -> what Entra gave us, plus the PKCE
        # challenge Claude sent. Entries live seconds and are used once.
        self._codes: dict[str, dict[str, Any]] = {}
        # The Entra sign-in in flight: our state -> Claude's request.
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- clients

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is not None:
            return client

        # A restart empties the dictionary above, and Claude still holds the
        # client id it registered - it would be told its client is unknown and
        # the connector would break until someone removed and re-added it.
        # Rebuilding a record for Claude's own callbacks keeps that from
        # happening. It is not a hole: the authorize handler still checks the
        # requested redirect_uri against this list, so a code can only ever be
        # sent to Claude, whoever asks for it.
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None,
            redirect_uris=[AnyUrl(u) for u in KNOWN_CLIENT_REDIRECTS],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=entra_auth.qualified_scope(),
        )

    # ----------------------------------------------------------- authorize

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Send the user to Microsoft, remembering how to get back to Claude."""
        state = secrets.token_urlsafe(32)
        self._pending[state] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "client_state": params.state,
            "resource": params.resource,
            "created": time.time(),
        }
        self._sweep()

        # Ask Entra for our own API scope, so the token comes back with us as
        # its audience - which is what makes the on-behalf-of exchange legal.
        query = urlencode({
            "client_id": entra_auth.CLIENT_ID,
            "response_type": "code",
            "redirect_uri": callback_url(),
            "response_mode": "query",
            "scope": f"{entra_auth.qualified_scope()} {UPSTREAM_SCOPES}",
            "state": state,
        })
        return f"{entra_auth.AUTHORITY}/oauth2/v2.0/authorize?{query}"

    async def handle_callback(self, code: str, state: str) -> str:
        """Entra has authenticated the user. Swap its code for a real token,
        then hand Claude a code of ours. Returns where to send the browser.

        Raises ValueError when the request cannot be tied back to a sign-in we
        started, which is the case an attacker would have to produce.
        """
        pending = self._pending.pop(state, None)
        if pending is None:
            raise ValueError("unknown or expired sign-in")

        async with httpx.AsyncClient(timeout=30) as http:
            res = await http.post(
                f"{entra_auth.AUTHORITY}/oauth2/v2.0/token",
                data={
                    "client_id": entra_auth.CLIENT_ID,
                    "client_secret": entra_auth.CLIENT_SECRET,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url(),
                    "scope": f"{entra_auth.qualified_scope()} {UPSTREAM_SCOPES}",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        body = res.json()
        if "access_token" not in body:
            # Do not put Entra's message in front of the user; it names
            # tenants and app ids. It goes to the log instead.
            print(f"  [auth] Entra refused the code: {body.get('error')}",
                  file=sys.stderr)
            raise ValueError("Microsoft did not complete the sign-in")

        our_code = secrets.token_urlsafe(32)
        self._codes[our_code] = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token"),
            "expires_in": int(body.get("expires_in", 3600)),
            "client_id": pending["client_id"],
            "code_challenge": pending["code_challenge"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "resource": pending["resource"],
            "expires_at": time.time() + 300,
        }

        back = {"code": our_code}
        if pending["client_state"] is not None:
            back["state"] = pending["client_state"]
        sep = "&" if "?" in pending["redirect_uri"] else "?"
        return f"{pending['redirect_uri']}{sep}{urlencode(back)}"

    # --------------------------------------------------------------- codes

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        entry = self._codes.get(authorization_code)
        if entry is None or entry["client_id"] != client.client_id:
            return None
        if entry["expires_at"] < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=[entra_auth.qualified_scope()],
            expires_at=entry["expires_at"],
            client_id=client.client_id,
            # The SDK checks this against the code_verifier for us.
            code_challenge=entry["code_challenge"],
            redirect_uri=AnyUrl(entry["redirect_uri"]),
            redirect_uri_provided_explicitly=entry["redirect_uri_provided_explicitly"],
            resource=entry["resource"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a code that could be redeemed twice is a replay.
        entry = self._codes.pop(authorization_code.code, None)
        if entry is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return OAuthToken(
            access_token=entry["access_token"],
            token_type="Bearer",
            expires_in=entry["expires_in"],
            scope=entra_auth.qualified_scope(),
            refresh_token=entry["refresh_token"],
        )

    # ------------------------------------------------------------ refresh

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        # Entra's refresh tokens are opaque to us and we keep no copy, so
        # there is nothing to look up - we accept it here and find out whether
        # it is real when Entra answers below.
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=[entra_auth.qualified_scope()],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with httpx.AsyncClient(timeout=30) as http:
            res = await http.post(
                f"{entra_auth.AUTHORITY}/oauth2/v2.0/token",
                data={
                    "client_id": entra_auth.CLIENT_ID,
                    "client_secret": entra_auth.CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token.token,
                    "scope": f"{entra_auth.qualified_scope()} {UPSTREAM_SCOPES}",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        body = res.json()
        if "access_token" not in body:
            print(f"  [auth] refresh refused: {body.get('error')}", file=sys.stderr)
            raise TokenError("invalid_grant", "the sign-in has expired")
        return OAuthToken(
            access_token=body["access_token"],
            token_type="Bearer",
            expires_in=int(body.get("expires_in", 3600)),
            scope=entra_auth.qualified_scope(),
            refresh_token=body.get("refresh_token", refresh_token.token),
        )

    # ------------------------------------------------------------- tokens

    async def load_access_token(self, token: str) -> AccessToken | None:
        # The token is Entra's, so the existing verifier checks its signature,
        # issuer, audience, expiry and scope.
        return await entra_auth.EntraTokenVerifier().verify_token(token)

    async def revoke_token(self, token: Any) -> None:
        # Entra has no revocation endpoint for these, and we hold no copy to
        # throw away. Signing out in Claude discards its own tokens.
        return None

    # -------------------------------------------------------------- upkeep

    def _sweep(self) -> None:
        """Drop sign-ins nobody completed, so the dictionaries stay small."""
        cutoff = time.time() - 600
        for state, p in list(self._pending.items()):
            if p["created"] < cutoff:
                del self._pending[state]
        now = time.time()
        for code, e in list(self._codes.items()):
            if e["expires_at"] < now:
                del self._codes[code]
