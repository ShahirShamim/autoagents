# autoagents

A messaging-driven, multi-tenant autonomous assistant. Each tenant gets a personal AI
agent they reach over **email** and **WhatsApp**; the agent reads their messages and
attachments (text, images, PDFs, audio, short video — in any language), remembers facts
across conversations, searches their private document store, schedules follow-ups, and
acts on their behalf (drafting/sending email + WhatsApp, coordinating with third parties)
— always with confirmation gates on bulk sends and web search.

Built on Google's Agent Development Kit (ADK) on GCP. Private beta.

- **Operator guide:** [`docs/HUMAN_GUIDE.md`](docs/HUMAN_GUIDE.md)
- **Reproduction / ops spec:** [`docs/AGENT_GUIDE.md`](docs/AGENT_GUIDE.md)
- **Multi-tenant design:** [`docs/MULTI_TENANT_PLAN.md`](docs/MULTI_TENANT_PLAN.md), [`docs/MULTI_TENANT_SCOPE.md`](docs/MULTI_TENANT_SCOPE.md)

---

## 1. Technical specification

### 1.1 Capabilities

| Capability | Notes |
|---|---|
| Multi-channel intake | Email (Resend inbound webhook) + WhatsApp (Baileys bridge) |
| Multimodal reasoning | Gemini reads images / PDFs / audio / short video attachments; replies in English regardless of input language |
| Long-term memory | ADK Memory Bank (facts/preferences recalled across sessions) |
| Private knowledge (RAG) | Per-tenant Vertex RAG Engine corpus; ingest by `gs://` URI, retrieve on demand |
| Web search | Grounded Google search — **gated behind explicit per-request user consent** |
| Actions | Send email / WhatsApp on the user's behalf; **bulk-send confirmation gate** |
| Third-party threads | Agent emails/messages contacts from a tagged/owned identity; replies relayed to the owner within a **3-hour window** |
| Scheduling | Schedule / list / cancel reminders + follow-ups; Cloud Scheduler tick drives due tasks |
| Per-tenant agent context | Operator-authored standing instructions prepended to every turn (admin panel) |
| Self-service WhatsApp linking | Tenant scans a QR from an emailed magic link to attach their **own dedicated** number |

### 1.2 Architecture

```
     Email (Resend)        WhatsApp (Baileys)              Operator (browser)
          │                        │                             │
          ▼ webhook                ▼ inbound POST                ▼ magic-link auth
  ┌─────────────────────────────────────────┐         ┌───────────────────────┐
  │      Cloud Run: autoagents-gateway       │         │  Cloud Run: admin     │
  │      FastAPI event/routing layer         │         │  autoagents-admin     │
  │      https://autoagents.jmkn.tech        │         │ admin.autoagents.     │
  │  · identity → tenant resolution          │         │        jmkn.tech      │
  │  · owner vs third-party gating           │         │  · tenant CRUD        │
  │  · magic-link QR pages + proxy           │         │  · run-state controls │
  │  · scheduled-task runner                 │         │  · per-tenant context │
  └───────────────┬─────────────────────────┘         └──────────┬────────────┘
                  │ query_agent (session per tenant)               │ Firestore RW
                  ▼                                                 │
  ┌─────────────────────────────────────────┐                     │
  │   Agent Runtime (Vertex reasoningEngine) │                     │
  │   ADK root_agent · Gemini gemini-3.5-flash│                    │
  │   tools: search/ingest docs, web_search, │                     │
  │   send_email, send_whatsapp, schedule/   │                     │
  │   list/cancel_task, query_messages,      │                     │
  │   get/set_agent_state, current_time      │                     │
  └───────────────┬─────────────────────────┘                     │
                  │                                                 │
     ┌────────────┼───────────────┬─────────────────┬──────────────┘
     ▼            ▼               ▼                 ▼
  Firestore   RAG corpora     GCS attachments   WhatsApp bridge
  (default)   (per-tenant,    bucket            (e2-micro VM, Node/Baileys
              us-west1)                          multi-session: 1 socket/tenant)
```

**Three deployables + a bridge:**

1. **Agent Runtime** — the ADK "brain" (`autoagents-agent/`), deployed as a Vertex AI
   Agent Engine (`reasoningEngine`). Stateless per request; state lives in ADK sessions +
   Memory Bank + Firestore. Model: `gemini-3.5-flash` (multimodal).
2. **Gateway** — Cloud Run FastAPI service (`gateway/`). The event layer: receives inbound
   email/WhatsApp, resolves the tenant, enforces owner/third-party + consent/TTL rules,
   invokes the agent, sends replies, serves the WhatsApp-linking pages, runs scheduled tasks.
3. **Admin** — Cloud Run FastAPI service (`admin/`). Operator console: tenant CRUD,
   identities, per-agent run-state, analytics, per-tenant agent context. Email magic-link auth.
4. **WhatsApp bridge** — Node/Baileys (`whatsapp-bridge/`) on an always-on **e2-micro** VM.
   **Multi-session**: one Baileys socket per tenant, so the WhatsApp account *is* the tenant
   boundary. Per-tenant auth persisted to GCS as a single gzipped blob.

### 1.3 Repository layout

```
autoagents-agent/     ADK agent (app/agent.py, app/tools.py, retrievers, config)
                      tests/post_deploy.py — live end-to-end suite; deploy.sh
gateway/              FastAPI event layer (main.py, clients.py, tenancy.py, config.py)
admin/                FastAPI operator console (main.py, tenancy.py, config.py)
whatsapp-bridge/      Node/Baileys multi-session bridge (index.js)
docs/                 AGENT_GUIDE, HUMAN_GUIDE, MULTI_TENANT_PLAN/SCOPE
Design/               JMKN design system (used by admin UI + pitch)
pitches/              Client pitch deck (HTML)
```

### 1.4 Tech stack

- **Agent:** Google ADK (`google-agents-cli`), Vertex AI Agent Engine, Gemini `gemini-3.5-flash`, Vertex RAG Engine (Basic tier), ADK Memory Bank.
- **Services:** Python 3.12, FastAPI + Uvicorn, Cloud Run (source deploys via Cloud Build + Dockerfile).
- **Bridge:** Node.js, [Baileys](https://github.com/WhiskeySockets/Baileys) (unofficial WhatsApp Web), on a GCE e2-micro.
- **Data:** Firestore (Native), Cloud Storage, Vertex RAG corpora, Secret Manager.
- **Messaging:** [Resend](https://resend.com) (email send + inbound webhook, Svix-signed), Baileys (WhatsApp).
- **Scheduling:** Cloud Scheduler (`*/5 * * * *` → gateway `/tasks/run`).
- **DNS/TLS:** Cloud Run domain mappings; Cloudflare DNS (CNAME → `ghs.googlehosted.com`, DNS-only); Google-managed certs.

### 1.5 Deployment (live)

| Component | Identity |
|---|---|
| GCP project | `autoagents-500500` (number `323512451403`), region `us-central1` |
| Agent engine | `reasoningEngines/5931933951328256000` |
| Gateway | Cloud Run `autoagents-gateway` → **`https://autoagents.jmkn.tech`** (public) |
| Admin | Cloud Run `autoagents-admin` → **`https://admin.autoagents.jmkn.tech`** (public; app-auth) |
| WhatsApp bridge | GCE `autoagents-wa` (e2-micro, `us-central1-a`), static IP `:8080` |
| RAG | Vertex RAG Engine corpora in `us-west1` (Basic tier), one per tenant |
| Sender | `assistant@jmkn.tech` (Resend domain `jmkn.tech`, send + inbound) |
| Service account | `autoagents-gateway@…` (gateway + admin); runtime SA `…@gcp-sa-aiplatform-re…` |

> **Budget posture:** no always-on cost except the e2-micro (free-tier) bridge. Cloud Run
> scales to zero; one shared agent engine + per-tenant RAG corpora.

### 1.6 Data model (Firestore, `(default)` database)

| Collection | Doc id | Purpose |
|---|---|---|
| `tenants` | `<tenant_id>` | name, status (`pending`/`active`/`disabled`), `emails[]`, `phones[]`, `rag_corpus`, `wa_linked`/`wa_number`, `agent_context`, `notes` |
| `identities` | `email:<addr>` / `phone:<digits>` | routing key → `tenant_id` (inbound sender resolution) |
| `agent_state` | `<tenant_id>` | per-agent run-state: `running` / `paused` / `stopped` |
| `agent_sessions` | `<tenant_id>` | pointer to the tenant's live ADK session + `last_at` (idle rotation) |
| `messages` | auto | inbound/outbound audit log (channel, direction, from/to, status) |
| `tasks` | auto | scheduled reminders/follow-ups (`due_at`, `status`, `description`) |
| `threads` | `<tenant>:<channel>:<contact>` | third-party reply window state (`first_reply_at`, `expires_at`, `closed_notified`) |
| `usage` | auto | per-turn token accounting (prompt/output/thoughts/total) |
| `alerts` | auto | operational alerts surfaced in admin |

**Isolation:** long-term memory, RAG documents, tasks, run-state, and message logs are all
scoped by `tenant_id`. RAG corpora are physically distinct per tenant. Verified by a live
two-tenant leak test (`test_no_context_leak`).

### 1.7 Key request flows

**Inbound (owner email / WhatsApp).** Resolve sender identity → tenant → check run-state →
`ensure_session` (rotates after idle, flushing to Memory Bank first) → prepend the tenant's
`agent_context` → `query_agent` (attachments passed as file parts) → reply on the same channel.

**Third-party reply.** The agent contacts others from a tagged email (`assistant+<tenant>@…`)
or the tenant's own WhatsApp line. A reply is relayed to the owner **only if** there was a
prior outbound to that contact (else dropped as unsolicited), and **only within 3 hours** of
their first reply (`apply_thread_ttl`); on expiry the contact gets one "conversation closed"
note, then silence. A fresh outbound reopens the window.

**Per-tenant WhatsApp linking.** Admin (or onboarding) emails a signed magic link
(`/link?token=…`, itsdangerous, **expires 24h after sending**). The page walks the tenant
through linking a **second/dedicated** WhatsApp account (not their personal number) via
Linked Devices → QR. The bridge holds one Baileys socket per tenant; on connect the gateway
writes `wa_linked`/`wa_number` to the tenant doc.

**Admin auth.** Email **magic link** restricted to a single allowlisted address: enter email
→ neutral response (allowlist not probeable) → signed 15-min token emailed → `/auth` redeems
it → 8-hour signed session cookie. A break-glass password remains as an emergency fallback.
Session + tokens signed by `MAGIC_SECRET`, decoupled from the password.

**Scheduled tasks.** Cloud Scheduler pings `/tasks/run` every 5 min (token-gated); due tasks
for `running` tenants are executed by their agent.

### 1.8 Gateway HTTP endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | — | liveness |
| POST | `/inbound/email` | Svix HMAC | Resend inbound email webhook |
| POST | `/inbound/whatsapp` | `X-WA-Secret` | Baileys bridge inbound |
| GET | `/link` | signed token | WhatsApp-linking QR page |
| GET | `/link/{token}/status` · `/qr` | signed token | pairing status / QR (proxied to bridge) |
| POST | `/link/{token}/unlink` | signed token | unlink / change number |
| POST | `/internal/wa-link/{tenant}` | `X-Tasks-Token` | mint + email a linking magic link |
| POST | `/internal/ensure-corpus/{tenant}` | `X-Tasks-Token` | provision/report a tenant RAG corpus |
| POST | `/tasks/run` | `X-Tasks-Token` | run due scheduled tasks (Cloud Scheduler) |

### 1.9 Configuration (env / secrets)

Runtime config lives in **Secret Manager** and is wired as `secretEnv` (sticky across deploys).
Secrets: `resend-api-key`, `resend-webhook-secret`, `tasks-token`, `whatsapp-bridge-secret`,
`whatsapp-bridge-url`, `link-secret`, `admin-password` (break-glass), `admin-magic-secret`.
Notable env: `GATEWAY_PUBLIC_URL`, `LINK_MAX_AGE_HOURS=24`, `THREAD_TTL_HOURS=3`,
`ADMIN_EMAIL`, `ADMIN_PUBLIC_URL`, `COOKIE_SECURE=true`.

> **Security note:** secrets are never committed; `.env` is gitignored. Several beta secrets
> were surfaced in development chat and are slated for rotation before public launch.

### 1.10 Deploy

```bash
# Agent Runtime (requires human approval) — wraps agents-cli deploy, re-passing --secrets
cd autoagents-agent && ./deploy.sh

# Gateway / Admin (Cloud Run, source deploy via each service's Dockerfile)
gcloud run deploy autoagents-gateway --source ./gateway --region us-central1
gcloud run deploy autoagents-admin   --source ./admin   --region us-central1

# WhatsApp bridge (build → push → deploy by digest → reset the VM; see docs/AGENT_GUIDE.md)
```

Deploy gotchas (see `docs/AGENT_GUIDE.md` PITFALLS): use **absolute** `--source` paths;
`secretEnv` survives bare deploys but plain env does not (prefer `deploy.sh`); the VM's
konlet caches `:latest`, so deploy the bridge **by image digest** and `instances reset`.

### 1.11 Testing

Live end-to-end smoke suite against the **deployed** gateway + Firestore + RAG
(ephemeral `pdt_` tenants, torn down after — including their RAG corpus):

```bash
cd autoagents-agent && uv run pytest tests/post_deploy.py -v -s
```

Covers: onboarding, owner email/WhatsApp turns, per-tenant agent-context injection,
third-party email/WhatsApp relay, unsolicited-drop, the 3-hour TTL, long-term RAG storage,
and cross-tenant no-leak. (10 tests.)

---

## 2. Status

Private beta. Unofficial WhatsApp (Baileys) carries per-account ban risk and requires the
linked phone to come online periodically. The official WhatsApp Cloud API is the intended
public-scale path.
