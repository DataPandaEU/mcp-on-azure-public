# MCP server for a public HTTPS host. Anthropic's cloud dials this, so it
# must be reachable from the internet; the key or the sign-in is what keeps
# it closed.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py /app/

# No Azure CLI in here. Anything that needs Azure uses the container's own
# managed identity through DefaultAzureCredential.
ENV PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Never bake secrets into the image. They are set on the container app:
#   ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET,
#   PUBLIC_URL, ALLOWED_HOSTS

EXPOSE 8000
CMD ["python", "server.py", "--http"]
