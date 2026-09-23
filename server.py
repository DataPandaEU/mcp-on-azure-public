"""A minimal MCP server that runs in Azure Container Apps with Microsoft sign-in.

This is the template behind the guide in README.md. It has two tools, which is
all a template needs:

  hello(name)   proves the connection works
  whoami()      proves the sign-in works by naming the signed-in person

Replace those two with your own tools. The Microsoft sign-in, callback route,
health probe and transport can remain unchanged.

Clients authenticate with a Microsoft Entra bearer token. The HTTP server
refuses to start when the required Entra settings are missing, so it cannot be
accidentally exposed without authentication.

Run:
  python server.py --selftest      lists the tools, no network
  python server.py                 stdio, for a local Claude Desktop
  python server.py --http          Streamable HTTP on $PORT/mcp, for Azure
"""

import logging
import os
import sys

# stdio MCP speaks JSON-RPC over stdout. Anything else printed there breaks
# the connection silently, so every log line goes to stderr.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
for noisy in ("httpx", "httpcore", "mcp", "msal"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from mcp.server import MCPServer  # noqa: E402

import entra_auth  # noqa: E402

SERVER_NAME = os.getenv("SERVER_NAME", "my-mcp-server")

INSTRUCTIONS = """You are connected to a template MCP server hosted on Azure.
Two tools: hello greets, whoami says who the server thinks is calling. Use
whoami when someone asks whether the sign-in worked."""

bridge = None  # set by _auth_kwargs when the sign-in bridge is active


def _auth_kwargs() -> dict:
    """Microsoft sign-in, when it is configured.

    With ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET and PUBLIC_URL
    set, this server is the authorization server Claude talks to, and it
    brokers to Entra behind it (oauth_bridge.py). A client pastes the URL and
    signs in with Microsoft.
    """
    global bridge
    if not entra_auth.validation_configured():
        return {}

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from pydantic import AnyHttpUrl

    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not public_url:
        print("  [auth] PUBLIC_URL not set - sign-in disabled", file=sys.stderr)
        return {}

    common = dict(
        resource_server_url=AnyHttpUrl(public_url),
        required_scopes=[entra_auth.qualified_scope()],
        # Our verifier checks the audience itself. The SDK's own check would
        # compare it to this container's URL, which an Entra token never
        # carries; leaving it on would refuse every valid sign-in.
        validate_token_resource=False,
    )

    import oauth_bridge

    if oauth_bridge.configured():
        bridge = oauth_bridge.EntraOAuthBridge()
        print("  [auth] sign-in: this server, brokering to Microsoft", file=sys.stderr)
        return {
            "auth_server_provider": bridge,
            "auth": AuthSettings(
                issuer_url=AnyHttpUrl(public_url),
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=[entra_auth.qualified_scope()],
                    default_scopes=[entra_auth.qualified_scope()],
                ),
                **common,
            ),
        }

    print("  [auth] sign-in: Entra directly - clients need the client ID", file=sys.stderr)
    return {
        "token_verifier": entra_auth.EntraTokenVerifier(),
        "auth": AuthSettings(issuer_url=AnyHttpUrl(entra_auth.ISSUER), **common),
    }


server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version="0.1.0", **_auth_kwargs())


# ------------------------------------------------------------------ tools
# Replace these two with yours. Keep them returning short text: a tool result
# is conversation context, and a large blob crowds out the reasoning.

@server.tool(description="Say hello. Proves the connection works.")
def hello(name: str = "world") -> str:
    return f"Hello, {name}. This answer came from {SERVER_NAME} on Azure."


@server.tool(description="Who does the server think is calling? Names the signed-in person.")
def whoami() -> str:
    return entra_auth.describe_mode()


# ------------------------------------------------------------------ routes

@server.custom_route("/health", methods=["GET"])
async def health(request):
    """Open, so Container Apps can probe it."""
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok")


@server.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request):
    """Where Microsoft sends the browser after the person signs in.

    Runs in the user's browser, so nothing technical goes on screen; the
    reason for a failure goes to the log.
    """
    from starlette.responses import PlainTextResponse, RedirectResponse

    if bridge is None:
        return PlainTextResponse("Sign-in is not enabled on this server.", status_code=404)
    if request.query_params.get("error"):
        print(f"  [auth] Microsoft returned {request.query_params['error']}", file=sys.stderr)
        return PlainTextResponse("Microsoft did not complete the sign-in. Close this tab "
                                 "and try connecting again.", status_code=400)
    code, state = request.query_params.get("code"), request.query_params.get("state")
    if not code or not state:
        return PlainTextResponse("That sign-in link is incomplete. Close this tab and "
                                 "try connecting again.", status_code=400)
    try:
        target = await bridge.handle_callback(code, state)
    except ValueError as e:
        print(f"  [auth] callback rejected: {e}", file=sys.stderr)
        return PlainTextResponse("That sign-in link has expired. Close this tab and try "
                                 "connecting again.", status_code=400)
    return RedirectResponse(target, status_code=302)


# ------------------------------------------------------------------- main

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print("tools:", [t.name for t in server._tool_manager.list_tools()])
        print(hello("selftest"))
        print(whoami())
    elif "--http" in sys.argv:
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings

        public_url = os.getenv("PUBLIC_URL", "").strip()
        if not entra_auth.configured() or not public_url:
            raise SystemExit(
                "HTTP mode requires ENTRA_TENANT_ID, ENTRA_CLIENT_ID, "
                "ENTRA_CLIENT_SECRET and PUBLIC_URL. Refusing to start an "
                "unprotected MCP endpoint."
            )

        port = int(os.getenv("PORT", "8000"))
        # Behind the Container Apps ingress the Host header is the public
        # hostname. The SDK rejects unknown hosts (HTTP 421) unless told.
        allowed = [h for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h]
        app = server.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=bool(allowed),
                allowed_hosts=allowed,
                allowed_origins=["*"],
            ),
            host="0.0.0.0",
        )
        print(f"{SERVER_NAME} on :{port}/mcp  [Microsoft Entra sign-in required]", file=sys.stderr)
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    else:
        server.run(transport="stdio")
