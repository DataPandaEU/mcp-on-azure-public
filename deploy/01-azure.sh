#!/usr/bin/env bash
# Create the Azure resources and build the MCP image.
#
# The registry Admin user stays disabled. The Container App receives a
# system-assigned managed identity and the pull-only AcrPull role. The app
# starts with a public quickstart image; deploy/03-configure.sh switches it to
# the protected MCP only after the Entra settings are ready.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env - copy .env.example and fill in NAME"; exit 1; }
set -a; . ./.env; set +a
: "${NAME:?set NAME in .env}"; : "${LOCATION:=westeurope}"

RG="$NAME-rg"; ACR="${NAME}acr"; ENV="$NAME-env"; APP="$NAME-mcp"; IMAGE="$APP:v1"

az extension show -n containerapp >/dev/null 2>&1 || az extension add -n containerapp -y >/dev/null

echo "==> 1. resource group $RG"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> 2. private container registry $ACR (Admin user disabled)"
if az acr show -n "$ACR" -g "$RG" -o none 2>/dev/null; then
  az acr update -n "$ACR" -g "$RG" --admin-enabled false -o none
else
  az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled false -o none
fi

echo "==> 3. build and store $IMAGE (no Docker needed locally)"
az acr build --registry "$ACR" --image "$IMAGE" --file Dockerfile . --only-show-errors

echo "==> 4. Container Apps environment $ENV"
az containerapp env show -n "$ENV" -g "$RG" -o none 2>/dev/null \
  || az containerapp env create -n "$ENV" -g "$RG" -l "$LOCATION" -o none

echo "==> 5. container app $APP with a temporary public image"
if az containerapp show -n "$APP" -g "$RG" -o none 2>/dev/null; then
  az containerapp identity assign -n "$APP" -g "$RG" --system-assigned -o none
  az containerapp update -n "$APP" -g "$RG" --min-replicas 1 --max-replicas 1 -o none
else
  az containerapp create -n "$APP" -g "$RG" --environment "$ENV" \
    --image mcr.microsoft.com/k8se/quickstart:latest \
    --target-port 80 --ingress external \
    --min-replicas 1 --max-replicas 1 \
    --system-assigned \
    -o none
fi

FQDN=$(az containerapp show -n "$APP" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
MI=$(az containerapp identity show -n "$APP" -g "$RG" --query principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR" -g "$RG" --query id -o tsv)

echo "==> 6. allow the app identity to download images from the registry"
if ! az role assignment list --scope "$ACR_ID" \
  --query "[?principalId=='$MI' && roleDefinitionName=='AcrPull'].id | [0]" -o tsv | grep -q .; then
  az role assignment create --assignee-object-id "$MI" \
    --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID" -o none
fi

# Remember the public address for the Entra registration and final deployment.
grep -q '^PUBLIC_URL=' .env \
  && sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://$FQDN#" .env \
  || echo "PUBLIC_URL=https://$FQDN" >> .env

cat <<TXT

  Azure resources ready.

  MCP image          $ACR.azurecr.io/$IMAGE
  Temporary app      https://$FQDN
  Managed identity   $MI
  Registry Admin     disabled
  Scale              one replica

  The custom MCP is not public yet. Next run deploy/02-entra.sh, then
  deploy/03-configure.sh to start the image with Entra sign-in enabled.
TXT
