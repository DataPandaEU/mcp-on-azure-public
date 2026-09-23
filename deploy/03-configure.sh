#!/usr/bin/env bash
# Store the Entra settings and replace the temporary image with the protected
# MCP. The image is never started without Microsoft sign-in configured.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${NAME:?}"; : "${ENTRA_TENANT_ID:?run deploy/02-entra.sh first}"
: "${ENTRA_CLIENT_ID:?}"; : "${ENTRA_CLIENT_SECRET:?}"; : "${PUBLIC_URL:?}"

RG="$NAME-rg"; ACR="${NAME}acr"; APP="$NAME-mcp"; IMAGE="$APP:v1"
FQDN=${PUBLIC_URL#https://}

echo "==> 1. store the Entra client secret"
az containerapp secret set -n "$APP" -g "$RG" \
  --secrets entra-client-secret="$ENTRA_CLIENT_SECRET" -o none

echo "==> 2. configure managed-identity access to the private registry"
az containerapp registry set -n "$APP" -g "$RG" \
  --server "$ACR.azurecr.io" --identity system -o none

echo "==> 3. start the MCP image with Entra sign-in enabled"
az containerapp ingress update -n "$APP" -g "$RG" --target-port 8000 -o none
az containerapp update -n "$APP" -g "$RG" \
  --image "$ACR.azurecr.io/$IMAGE" \
  --set-env-vars \
    ENTRA_TENANT_ID="$ENTRA_TENANT_ID" \
    ENTRA_CLIENT_ID="$ENTRA_CLIENT_ID" \
    ENTRA_CLIENT_SECRET=secretref:entra-client-secret \
    PUBLIC_URL="$PUBLIC_URL" \
    ALLOWED_HOSTS="$FQDN" \
    SERVER_NAME="$APP" \
  --min-replicas 1 --max-replicas 1 \
  -o none

echo "==> 4. point the startup and liveness checks at /health on port 8000"
CONTAINER=$(az containerapp show -n "$APP" -g "$RG" \
  --query properties.template.containers[0].name -o tsv)
PROBE_FILE=$(mktemp)
trap 'rm -f "$PROBE_FILE"' EXIT
cat > "$PROBE_FILE" <<YAML
properties:
  template:
    containers:
      - name: $CONTAINER
        probes:
          - type: startup
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          - type: liveness
            httpGet:
              path: /health
              port: 8000
            periodSeconds: 30
YAML
az containerapp update -n "$APP" -g "$RG" --yaml "$PROBE_FILE" -o none

echo "==> 5. wait for the revision, then check health and OAuth discovery"
sleep 20
echo "    health      -> $(curl -s "$PUBLIC_URL/health")"
echo "    no token    -> $(curl -s -o /dev/null -w '%{http_code}' -X POST "$PUBLIC_URL/mcp")   (expect 401)"
echo "    discovery   -> $(curl -s "$PUBLIC_URL/.well-known/oauth-protected-resource")"

cat <<TXT

  The protected MCP is running.

  Add $PUBLIC_URL/mcp as a custom connector in Claude Desktop, choose Connect
  and sign in with your Microsoft account. No shared key or registry password
  is used.
TXT
