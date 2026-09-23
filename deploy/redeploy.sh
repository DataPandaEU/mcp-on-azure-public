#!/usr/bin/env bash
# Step 11 of the guide: ship a code change. Builds the next image version in
# the registry and points the container app at it.
#
#   deploy/redeploy.sh v2
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${NAME:?}"
TAG="${1:?usage: deploy/redeploy.sh vN}"
RG="$NAME-rg"; ACR="${NAME}acr"; APP="$NAME-mcp"

az acr build --registry "$ACR" --image "$APP:$TAG" --file Dockerfile . --only-show-errors
az containerapp update -n "$APP" -g "$RG" --image "$ACR.azurecr.io/$APP:$TAG" -o none

echo "waiting for the new revision to take traffic..."
sleep 20
az containerapp revision list -n "$APP" -g "$RG" \
  --query "[?properties.active].{revision:name, image:properties.template.containers[0].image, traffic:properties.trafficWeight}" -o table
echo
echo "no credentials -> $(curl -s -o /dev/null -w '%{http_code}' -X POST "$PUBLIC_URL/mcp")   (expect 401)"
echo "Then in Claude Desktop: Settings, Connectors, the connector, Disconnect, Connect - it caches the tool list."
