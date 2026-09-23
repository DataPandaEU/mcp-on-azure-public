"""Microsoft sign-in for the MCP server, and acting on behalf of the user.

Two halves, and they are easy to conflate:

  validation   Claude presents a token whose audience is *this server*
               (api://<client-id>/access_as_user). We check its signature
               against Entra's public keys, plus issuer, audience and expiry.

  on-behalf-of We cannot call Fabric with that token - its audience is us,
               not Fabric. So we exchange it, as the user, for a token whose
               audience is Fabric or Foundry. That exchange is what makes the
               audit log in Fabric show the person's name instead of ours.

Each MCP request carries a Microsoft Entra bearer token and runs as the
signed-in user. Exchange that token for whatever a tool needs with
obo_token(scope).
"""

import os
import sys
import time
from functools import lru_cache

import jwt
import msal
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")
CLIENT_ID = os.getenv("ENTRA_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("ENTRA_CLIENT_SECRET", "")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
JWKS_URL = f"{AUTHORITY}/discovery/v2.0/keys"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"

# What Claude asks for, and therefore what we must accept.
#
# Two spellings of one scope, and the difference matters. Entra's v2 endpoint
# wants custom API scopes fully qualified in the *authorization request* -
# `api://<client-id>/access_as_user` - and rejects the bare name, which it
# reserves for OIDC scopes. But the token it issues carries only the bare name
# in `scp`. So we advertise the qualified form, because that is what the client
# sends to Entra, and accept either when checking the token.
OUR_SCOPE = "access_as_user"


def qualified_scope() -> str:
    """The form Claude must send to Entra. A function, not a constant, because
    CLIENT_ID is read from the environment and rebound in tests."""
    return f"api://{CLIENT_ID}/{OUR_SCOPE}"


def _both_spellings(scopes: list[str]) -> list[str]:
    """Every scope in both forms, so the gate matches whichever it requires."""
    out = list(scopes)
    out += [f"api://{CLIENT_ID}/{s}" for s in scopes if not s.startswith("api://")]
    return out

# Downstream audiences you may exchange the user's token for, with
# obo_token(scope). Examples; add the ones your tools need, and add the
# matching delegated permission on the Entra app.
#   Microsoft Fabric   https://api.fabric.microsoft.com/.default
#   Microsoft Graph    https://graph.microsoft.com/.default
#   Azure AI Foundry   https://ai.azure.com/.default

def configured() -> bool:
    """Sign-in is only offered when all three settings are present."""
    return bool(TENANT_ID and CLIENT_ID and CLIENT_SECRET)


def validation_configured() -> bool:
    """Token validation needs no secret; only the OBO exchange does."""
    return bool(TENANT_ID and CLIENT_ID)


@lru_cache(maxsize=1)
def _jwks() -> PyJWKClient:
    # PyJWKClient caches the keys and refetches on rotation.
    return PyJWKClient(JWKS_URL)


@lru_cache(maxsize=1)
def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
    )


class EntraTokenVerifier(TokenVerifier):
    """Validates the token Claude presents, and keeps it for the OBO exchange.

    The raw token is carried on the AccessToken so tools can exchange it
    later. It never leaves this process and is never logged.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        if not validation_configured():
            return None
        try:
            signing_key = _jwks().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                # Audience is us. A token minted for Fabric or Graph must not
                # be accepted here just because it is a valid Entra token.
                audience=[CLIENT_ID, f"api://{CLIENT_ID}"],
                issuer=ISSUER,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as e:  # noqa: BLE001
            # Never echo the token or the reason to the caller; a validation
            # failure is a 401 either way.
            print(f"  [auth] token rejected: {type(e).__name__}", file=sys.stderr)
            return None

        scopes = (claims.get("scp") or "").split()
        if OUR_SCOPE not in scopes:
            print(f"  [auth] token lacks {OUR_SCOPE}", file=sys.stderr)
            return None

        return AccessToken(
            token=token,
            client_id=claims.get("appid") or CLIENT_ID,
            scopes=_both_spellings(scopes),
            expires_at=claims.get("exp"),
            subject=claims.get("oid") or claims.get("sub"),
            claims=claims,
        )


# --------------------------------------------------------------- user context

def current() -> AccessToken | None:
    """The signed-in user for this request."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
        return get_access_token()
    except Exception:  # noqa: BLE001
        return None


def current_user_name() -> str | None:
    """Who to name in the audit line."""
    tok = current()
    if tok is None:
        return None
    c = tok.claims or {}
    return c.get("preferred_username") or c.get("upn") or c.get("name") or tok.subject


# ------------------------------------------------------------- on-behalf-of

# Exchanges are not free, so hold them until shortly before they expire.
_obo_cache: dict[tuple[str, str], tuple[str, float]] = {}


def obo_token(scope: str) -> str | None:
    """A downstream token for the signed-in user, or None if not applicable.

    Returns None when nobody is signed in and when the exchange fails. A tool
    can decide whether a separate managed-identity path is appropriate.
    """
    if not configured():
        return None
    user = current()
    if user is None:
        return None
    key = (user.subject or "", scope)
    cached = _obo_cache.get(key)
    if cached and cached[1] > time.time() + 120:
        return cached[0]

    result = _msal_app().acquire_token_on_behalf_of(
        user_assertion=user.token, scopes=[scope]
    )
    if "access_token" not in result:
        # Most often the user has not consented to this downstream permission.
        print(f"  [auth] on-behalf-of failed for {scope}: "
              f"{result.get('error')}", file=sys.stderr)
        return None

    expires = time.time() + int(result.get("expires_in", 3600))
    _obo_cache[key] = (result["access_token"], expires)
    return result["access_token"]


def describe_mode() -> str:
    """One line for the audit and for --selftest."""
    name = current_user_name()
    if name:
        return f"signed in as {name}"
    return "no signed-in user"
