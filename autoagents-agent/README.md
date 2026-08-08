# autoagents-agent

The **brain** of [autoagents](../README.md) — an ADK agent deployed to Vertex AI Agent Runtime
(`reasoningEngines/5931933951328256000`). It is stateless per request: the Cloud Run gateway
resolves the tenant, opens/reuses an ADK session carrying `{tenant_id, rag_corpus}` in state,
and calls it. Every tool reads that state and scopes its Firestore / RAG access to that tenant.

Scaffolded with `agents-cli` 0.5.1 (`agentic_rag`), then heavily customised — treat the
generic scaffold commands below as a subset of what this project actually needs.

## Structure

```
app/
├── agent.py         # root_agent: model, instruction/persona, tool wiring
├── tools.py         # send_email, send_whatsapp, schedule/list/cancel_task,
│                    #   query_messages, get/set_agent_state, current_time,
│                    #   search_documents, ingest_document, web_search
├── config.py        # central config (coerces a numeric GOOGLE_CLOUD_PROJECT → project ID)
├── retrievers.py    # scaffold Vertex AI Search tool — superseded by RAG Engine, unused
└── mcp_server.py    # FastMCP: exposes the same tools over MCP (`uv sync --extra mcp`)
scripts/setup_rag_corpus.py   # idempotent RAG corpus provisioning
tests/post_deploy.py          # live end-to-end suite against the DEPLOYED stack (11 tests)
firestore.indexes.json        # composite indexes
deploy.sh                     # ALWAYS deploy with this — see below
CLAUDE.md                     # guidance for coding agents working in here
```

## Requirements

- **uv** — package manager ([install](https://docs.astral.sh/uv/getting-started/installation/))
- **agents-cli** — `uv tool install google-agents-cli`
- **gcloud** — authenticated, project set to `autoagents-500500`

```bash
agents-cli install     # sync deps
agents-cli playground  # local interactive testing
```

## Deployment

**Use `./deploy.sh`. Do not run a bare `agents-cli deploy`.**

```bash
cd autoagents-agent && ./deploy.sh   # requires explicit human approval
```

Two things a bare deploy gets wrong:

1. **Plain env vars are reset** on every deploy (`--secrets`/`secretEnv` is sticky, plain env
   is not). This is why WhatsApp used to break with "WhatsApp bridge not configured" — the
   bridge URL kept getting wiped. Runtime config therefore lives in Secret Manager
   (`resend-api-key`, `whatsapp-bridge-secret`, `whatsapp-bridge-url`) and `deploy.sh`
   re-passes it. If the bridge IP changes, update the `whatsapp-bridge-url` **secret**.
2. **`agents-cli deploy` strips `context_spec`** — which wipes the Memory Bank config: both
   the generation-model pin (`gemini-3.5-flash`; the unset default is the **deprecated**
   `gemini-2.5-flash`, shut down 2026-10-20) and the 3 managed memory topics. Re-apply the
   PATCH after every deploy — exact command in [`../docs/AGENT_GUIDE.md`](../docs/AGENT_GUIDE.md)
   §10, "Memory Bank model pin".

Agent Runtime does **not** load `.env` — the deployed agent runs on `app/config.py` defaults
plus the engine's `env`/`secretEnv`. Editing `.env` and redeploying changes nothing in prod.

## Testing

```bash
uv run pytest tests/unit tests/integration      # offline
uv run pytest tests/post_deploy.py -v -s        # live E2E against the deployed stack
```

`post_deploy.py` creates ephemeral `pdt_` tenants and tears them down (including their RAG
corpus). It covers onboarding, both channels in/out, per-tenant context injection, third-party
relay + the 3h reply window, unsolicited-drop, long-term storage, and cross-tenant no-leak.
Needs gcloud auth; reads secrets from Secret Manager.

## Commands

| Command | Description |
|---|---|
| `agents-cli install` | Install dependencies via uv |
| `agents-cli playground` | Local development environment |
| `agents-cli lint` | Code quality checks |
| `agents-cli eval` | Evaluate agent behaviour (`--help` for subcommands) |
| `./deploy.sh` | Deploy to Agent Runtime **(use this, not `agents-cli deploy`)** |

## Observability

Telemetry exports to Cloud Trace and Cloud Logging
(`resource.type=aiplatform.googleapis.com/ReasoningEngine`). Per-turn token usage is metered by
the gateway into Firestore `usage` and surfaced per tenant in the admin console; operational
failures raise rows in `alerts`.
