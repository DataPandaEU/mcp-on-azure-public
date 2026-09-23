#!/usr/bin/env bash
# Create the Entra app registration used by the MCP sign-in.
#
# Creates one app with:
#   - a public-client redirect for Claude        https://claude.ai/api/mcp/auth_callback
#   - a web redirect for this server's callback  $PUBLIC_URL/oauth/callback
#   - an application ID URI and one scope         api://<client-id>/access_as_user
#   - a client secret, valid one year, written to .env
#
# Needs: PUBLIC_URL in .env (from 01-azure.sh) and the right to register apps
# in the tenant. Everything here is also doable by hand in the Entra portal;
# the guide shows both.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${NAME:?set NAME in .env}"; : "${PUBLIC_URL:?run deploy/01-azure.sh first}"
APP_NAME="$NAME-mcp"

TENANT=$(az account show --query tenantId -o tsv)

echo "==> 1. register the app (single tenant)"
if az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv | grep -q .; then
  CLIENT_ID=$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv)
  echo "    exists: $CLIENT_ID"
else
  CLIENT_ID=$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg \
    --query appId -o tsv)
  echo "    created: $CLIENT_ID"
fi
OBJECT_ID=$(az ad app show --id "$CLIENT_ID" --query id -o tsv)

echo "==> 2. redirect URIs: Claude and the MCP callback"
az ad app update --id "$CLIENT_ID" \
  --public-client-redirect-uris "https://claude.ai/api/mcp/auth_callback" "https://claude.com/api/mcp/auth_callback" \
  --web-redirect-uris "$PUBLIC_URL/oauth/callback" \
  --identifier-uris "api://$CLIENT_ID" -o none

echo "==> 3. expose the access_as_user scope"
SCOPE_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
cat > /tmp/api.json <<JSON
{
  "api": {
    "requestedAccessTokenVersion": 2,
    "oauth2PermissionScopes": [{
      "id": "$SCOPE_ID",
      "value": "access_as_user",
      "type": "User",
      "isEnabled": true,
      "adminConsentDisplayName": "Use $APP_NAME as the signed-in user",
      "adminConsentDescription": "Lets $APP_NAME act on behalf of the signed-in user.",
      "userConsentDisplayName": "Use $APP_NAME as you",
      "userConsentDescription": "Lets $APP_NAME act as you."
    }]
  }
}
JSON
# Only add the scope if none exists yet; Graph rejects replacing an enabled scope.
if [ "$(az ad app show --id "$CLIENT_ID" --query 'length(api.oauth2PermissionScopes)' -o tsv)" = "0" ]; then
  az rest --method PATCH --url "https://graph.microsoft.com/v1.0/applications/$OBJECT_ID" \
    --headers "Content-Type=application/json" --body @/tmp/api.json -o none
fi

echo "==> 4. create a one-year client secret. Record the expiry date."
SECRET=$(az ad app credential reset --id "$CLIENT_ID" --display-name "$APP_NAME-signin" \
  --years 1 --append --query password -o tsv)

# Save for 03-configure.sh
for kv in "ENTRA_TENANT_ID=$TENANT" "ENTRA_CLIENT_ID=$CLIENT_ID" "ENTRA_CLIENT_SECRET=$SECRET"; do
  k=${kv%%=*}
  grep -q "^$k=" .env && sed -i "s#^$k=.*#$kv#" .env || echo "$kv" >> .env
done

cat <<TXT

  Entra app ready.

  Tenant        $TENANT
  Client ID     $CLIENT_ID
  Scope         api://$CLIENT_ID/access_as_user
  Redirects     https://claude.ai/api/mcp/auth_callback (public), $PUBLIC_URL/oauth/callback (web)
  Secret        saved to .env, expires in one year - note the date

  If your tools call Microsoft services on the user's behalf (Fabric, Graph,
  Foundry...), add those delegated permissions under API permissions now.

  Next: deploy/03-configure.sh starts the protected MCP image.
TXT
