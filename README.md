# Host an MCP server on Azure with Microsoft sign-in

This repository contains a small MCP server and the files needed to run it in Azure Container Apps. It is a proof of concept for connecting Claude Desktop to an MCP hosted in your Azure environment and using Microsoft Entra ID for sign-in.

The example has two tools:

- `hello` returns a greeting and confirms that Claude can call the MCP.
- `whoami` returns the Microsoft account used during sign-in and confirms that Entra authentication works.

You can use the example as it is or replace the tools with your own. GitHub is useful for teamwork and version history, but it is not required for the Azure build. You can also download the source to a local folder or Azure Cloud Shell.

## Recommended security route

This example uses one route throughout:

- The Azure Container Registry Admin user stays disabled.
- Azure Container Registry builds and stores the image.
- The Container App has a system-assigned managed identity.
- The identity receives the pull-only `AcrPull` role so it can download the image.
- People connecting through Claude sign in with Microsoft Entra ID.
- The MCP refuses to start in HTTP mode if the Entra settings are missing.
- The proof of concept runs one replica because the temporary OAuth state is kept in memory.

The registry identity and user sign-in solve different problems. `AcrPull` allows the Container App to download its image. Entra authentication controls who may use the MCP. No registry password or shared MCP key is used.

## Repository contents

| File | Purpose |
|---|---|
| `server.py` | MCP server, `hello` and `whoami` tools, health endpoint and OAuth callback |
| `entra_auth.py` | Checks Entra access tokens and supports on-behalf-of access for future tools |
| `oauth_bridge.py` | Connects the MCP OAuth flow to Microsoft Entra |
| `requirements.txt` | Python packages installed in the image |
| `Dockerfile` | Builds the Python container image and starts the MCP on port 8000 |
| `.dockerignore` | Keeps local, documentation and sensitive files out of the Azure build context |
| `deploy/` | Optional scripts that follow the same Entra-only route as the guide |
| `docs/Hosting-an-MCP-server-on-Azure.docx` | Full step-by-step guide |

## Before you start

You need:

- An Azure subscription where you may create the required resources.
- Permission to build an image in Azure Container Registry and assign `AcrPull`.
- Permission to create or update an app registration in Microsoft Entra.
- Azure CLI access through Azure Cloud Shell or a local terminal.
- Claude Desktop on a plan that supports custom connectors.

## Follow the manual guide

The Word guide in `docs/` explains every step and the reason for each setting. It is the recommended route for a first proof of concept.

The image build command is:

```bash
az acr build --registry <registry-name> \
  --image <image-name>:v1 \
  --file Dockerfile .
```

The final dot is the build context. Azure uploads the current folder, reads the Dockerfile, builds the image and stores it in the registry. `.dockerignore` prevents unrelated files from being uploaded. The Dockerfile copies only `requirements.txt` and the root-level Python files into the finished image.

## Use the optional deployment scripts

The scripts are useful after you understand the manual steps. Run them from Bash in Azure Cloud Shell or another terminal with Azure CLI installed.

1. Copy `.env.example` to `.env`.
2. Set `NAME` to a short lowercase name. The registry name must be unique in Azure.
3. Sign in and select the correct Azure subscription.
4. Run the scripts in this order:

```bash
bash deploy/01-azure.sh
bash deploy/02-entra.sh
bash deploy/03-configure.sh
```

`01-azure.sh` creates the Azure resources, keeps the registry Admin user disabled, builds the image, creates the Container App with a temporary image and assigns `AcrPull` to its managed identity.

`02-entra.sh` creates the single-tenant Entra app registration, redirect addresses, `access_as_user` scope and client secret. It writes the resulting values to the gitignored `.env` file.

`03-configure.sh` stores the client secret as a Container Apps secret, adds the Entra settings, points the app at the private MCP image and keeps the replica count at one. The custom image is not started before authentication is configured.

After the final script, add `<Application-Url>/mcp` as a custom connector in Claude Desktop and sign in with your Microsoft account.

## Test the proof of concept

Try these instructions in Claude Desktop:

- "Use the Azure MCP hello tool with the name Alex."
- "Use the Azure MCP whoami tool and tell me which account is signed in."

If both tools respond, Claude can reach the MCP in Azure and Entra authentication is working.

The health endpoint remains public so Azure can check the container:

```text
https://<Application-Url>/health
```

A request to `/mcp` without a valid Entra token should return HTTP 401.

## Publish a later version

After changing the code, build a new tag and update the Container App:

```bash
bash deploy/redeploy.sh v2
```

Use a new tag for each release. This makes it clear which image a revision uses and makes rollback easier.

## Before production use

This repository is a learning example. Before wider use:

- Move temporary OAuth state to a shared store before enabling several replicas.
- Replace manual deployments with an approved Azure DevOps or GitHub Actions pipeline.
- Review whether your organization requires a certificate or federated credential instead of a client secret.
- Grant only the permissions each MCP tool needs.
- Add monitoring and a process for rotating credentials.

## License

Licensed under the Apache License 2.0. See `LICENSE`.
