# autoagents — Build Steps Log

Running log of every step taken to build the autoagents assistant.
Source of truth for *what the agent is* lives in `.agents-cli-spec.md`.

- **Project:** `autoagents-500500` (GCP, billing open)
- **Account:** jmkntech@gmail.com (holds $300 / 90-day trial credit)
- **Model:** `gemini-3.5-flash`
- **Region:** `us-central1`
- **Deploy:** Agent Runtime (brain) + Cloud Run (gateway)
- **Email:** Resend on domain `jmkn.tech` (Cloudflare DNS)

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked/needs user

---

## Phase 0 — Understand & Spec  `[x]`
- [x] Clarify requirements (WhatsApp→v2, calls→v2, email-only admin, Agent Runtime, $300 budget)
- [x] Confirm model `gemini-3.5-flash` exists (GA, multimodal)
- [x] Verify env: uv ✓, agents-cli ✓, gcloud ✓ (authed jmkntech@gmail.com), project `autoagents-500500`
- [x] Write `.agents-cli-spec.md`
- [ ] User approves spec  `[!]`

## Phase 1 — Prereqs & GCP setup  `[~]`
- [x] `gcloud config set project autoagents-500500`
- [x] Confirm trial billing linked to project (billingEnabled: true)
- [x] Enable APIs: aiplatform, run, cloudscheduler, firestore, storage, secretmanager, cloudbuild, artifactregistry, iam
- [x] Create Firestore (Native mode, us-central1)
- [x] Create GCS bucket `gs://autoagents-500500-attachments` (us-central1)
- [x] agents-cli auth confirmed (jmkntech@gmail.com, ADC quota project set)
- [x] Resend domain `jmkn.tech` verified — **sending enabled**; outbound test email sent + logged ✓
- [x] Store Resend API key in Secret Manager (`resend-api-key`)
- [ ] Enable **receiving** on jmkn.tech (MX records on Cloudflare) + create Resend webhook → gateway `/inbound/email` (post-deploy)
- [ ] Store `RESEND_WEBHOOK_SECRET` in Secret Manager (after webhook created)

## Phase 2 — Scaffold ADK project  `[x]`
- [x] `agents-cli scaffold create autoagents-agent --agent agentic_rag --datastore agent_platform_search --deployment-target agent_runtime --region us-central1 --agent-guidance-filename CLAUDE.md --prototype` (prototype-first; deploy added later via enhance)
- [x] `agents-cli install` (deps synced)
- [x] Project at `autoagents-agent/`, manifest confirms agent_runtime + agent_platform_search

## Phase 3 — Build agent brain  `[~]`
- [x] Root agent + instruction (gemini-3.5-flash)
- [x] Tools: send_email, schedule_task, list_tasks, cancel_task, query_messages, get/set_agent_state, current_time (`app/tools.py`)
- [x] `app/config.py` central config; `.env` local config (gitignored)
- [x] Firestore composite indexes (tasks: status+due_at, messages: channel+ts) + `firestore.indexes.json`
- [x] Smoke test: model + tool-calling + Firestore writes all verified via `agents-cli run`
- [x] **Vector store = RAG Engine** (user pick): corpus `…/us-west1/ragCorpora/4611686018427387904` (serverless/Basic tier; us-central1 capacity-restricted for new projects)
- [x] RAG tools `search_documents` + `ingest_document` — verified live (ingested sample PDF, retrieved by query)
- [x] **Memory Bank** wired natively: `PreloadMemoryTool()` + `after_agent_callback=add_session_to_memory` (activates on Agent Runtime; context_spec set at deploy)
- [x] `scripts/setup_rag_corpus.py` (idempotent corpus setup)
- [x] MCP server `app/mcp_server.py` (FastMCP, exposes all 10 tools; `uv sync --extra mcp`) — imports OK
- [x] **Multimodal VERIFIED**: gateway lists inbound attachments (`/emails/receiving/{id}/attachments` → `download_url`), downloads → GCS → passes as `file_data` parts to Agent Runtime. Tested with the real inbound image — agent correctly described a photo of a racing-game menu. Image/pdf/audio/video/text supported; others by reference.
  - Fixes: attachment bytes via `download_url` (not inline); `email_id` resolved with `latest_inbound_id()` fallback when webhook id isn't a retrievable inbound id; diagnostic logging added.

> Note: Vertex AI **Search** datastore from Phase 3.5 is now superseded by RAG Engine — leave (cheap/serverless) or destroy later. `vertex_search_tool` left defined-but-unused in agent.py for now.

## Phase 4 — Event gateway (Cloud Run / FastAPI)  `[~]`  (code written, not deployed)
- [x] `gateway/main.py` `/inbound/email` — verify signature, log, attachments→GCS, state check, admin parse, call agent, reply, log
- [x] `gateway/main.py` `/tasks/run` — scan due tasks, execute via agent, mark done
- [x] `/healthz`
- [x] `gateway/clients.py` Firestore + GCS + Resend + Agent Runtime helpers
- [x] Admin commands: !pause !resume !stop !status + sender allowlist
- [x] `gateway/Dockerfile` + `requirements.txt` + `.dockerignore`
- [ ] Finalise Agent Runtime query API (verify after deploy)
- [ ] Full multimodal: pass GCS media as inline parts to agent (currently refs in prompt)
- [ ] Deploy to Cloud Run (needs AGENT_ENGINE_RESOURCE from Phase 7)

## Phase 5 — Infra & schedule  `[~]`
- [x] Cloud Scheduler `autoagents-tasks-tick` → POST `/tasks/run` every 5 min (X-Tasks-Token header)
- [x] Resend webhook `7f422451…` (`email.received`) → gateway `/inbound/email`; signing secret → Secret Manager (`resend-webhook-secret`)
- [ ] Daily digest scheduler job (later)
- [ ] Optional: move ad-hoc gcloud resources into Terraform for repeatability

## Phase 6 — Evaluate  `[ ]`
- [ ] `agents-cli eval` core cases (round-trip, memory, RAG, schedule, control)
- [ ] Iterate to thresholds

## Phase 7 — Deploy  `[~]`
- [x] `agents-cli deploy` (Agent Runtime) — **LIVE**. Engine: `projects/323512451403/locations/us-central1/reasoningEngines/5931933951328256000`. Tested via SDK: tool-calling + Firestore access + Memory Bank all work.
- [x] Deploy Cloud Run gateway — **LIVE** at `https://autoagents-gateway-323512451403.us-central1.run.app` (public, user-approved). `/inbound/email` + `/tasks/run` (token-gated) verified reachable. Redeploy w/ webhook secret + metadata-fetch + `/health` rename in progress.
- [x] Granted Agent Runtime SA (`service-323512451403@gcp-sa-aiplatform-re`) roles: datastore.user, aiplatform.user, storage.objectAdmin, secretAccessor, logWriter
- [x] Gateway SA `autoagents-gateway@…` created + IAM
- [x] Cloud Scheduler → gateway `/tasks/run` ✓
- [x] Resend webhook (`email.received`) + signing secret stored ✓
- [x] **End-to-end verified:** `/health` 200; `/tasks/run` ran a past-due task → gateway queried deployed Agent Runtime → task marked `done`. Proves Scheduler→gateway→Agent Runtime→Firestore loop live.
- [x] Receiving **enabled** on root jmkn.tech (free plan = 1 domain; subdomain would need Resend Pro). Enabled via REST PATCH `{"receiving":true}`.
- [x] **USER added inbound MX** at Cloudflare (`@ MX inbound-smtp.us-east-1.amazonaws.com prio 10`) — propagated.
- [x] **INBOUND ROUND-TRIP VERIFIED LIVE** 🎉 gmail→assistant@jmkn.tech → gateway → agent replied → both logged. `/emails/receiving/{id}` retrieve path confirmed working (body present). Memory Bank stored a fact.

### Gateway findings
- Resend `email.received` webhook is **metadata-only** → gateway fetches body via `fetch_inbound_email(id)`.
- Google edge intercepts `/healthz` → health route renamed to `/health`.

## Phase 8 — Observe  `[ ]`
- [ ] Cloud Trace + prompt/response logging
- [ ] Confirm message/task/call logging in Firestore

## Phase 9 — WhatsApp (Baileys bridge)  `[x]`  ✅ LIVE
Method: **Baileys** (unofficial WhatsApp Web, same as openclaw). Dedicated number `+44 7340 926493`.
- [x] `whatsapp-bridge/` Node service (Baileys): GCS-persisted auth, `/qr` (token), `/send` (secret), `/health`, inbound→gateway, media→GCS
- [x] Image → Artifact Registry `us-central1-docker.pkg.dev/autoagents-500500/autoagents/whatsapp-bridge`
- [x] **e2-micro free VM** `autoagents-wa` (us-central1-a), static IP `136.114.229.113`, firewall tcp:8080, SA=autoagents-gateway
- [x] Secret `whatsapp-bridge-secret`; gateway `/inbound/whatsapp`; agent `send_whatsapp` tool
- [x] Gateway + agent redeployed with WHATSAPP_BRIDGE_URL/SECRET
- [x] Paired (live `/qr` page) + **inbound→agent→reply verified** ✓ (handles `@lid` addressing)

### WhatsApp gotchas (learned)
- VM service account needs **`roles/artifactregistry.reader`** or the container can't pull (`downloadArtifacts` denied).
- QR **rotates (~20-30s)** — a static screenshot fails ("couldn't link, try again"). Serve a **live auto-refreshing `/qr`** page; scan the on-screen QR.
- WhatsApp throttles after repeated failed link attempts → "try again later"; wait a few min.
- Post-scan close code **515** = "restart required" (normal); reconnect from saved creds, no re-pair.
- Baileys fires `creds.update` constantly → **debounce + sequential, non-resumable** GCS backup, else the 1GB e2-micro socket-hangs and HTTP flaps.
- `create-with-container` shows a deprecation warning (container-VM startup agent) — works for now.

## v2 (deferred)
- [ ] Voice calls (decide provider + budget)
- [ ] WhatsApp groups (DMs only in v1) + WA admin commands (ADMIN_WHATSAPP)
- [ ] Optional web dashboard

---

## Documentation
- `docs/HUMAN_GUIDE.md` — novice walkthrough: concepts, prerequisites, step-by-step setup, parameters, operations, troubleshooting.
- `docs/AGENT_GUIDE.md` — machine-oriented: exact ordered commands, verification checks, resource inventory, env/secrets/IAM, pitfalls.
- `docs/MULTI_TENANT_SCOPE.md` — scope for multi-user (agent-per-user) + admin webapp. Decisions LOCKED; not yet built.

## Activity Log
- 2026-06-25 — Phase 0 done. Clarified scope, confirmed `gemini-3.5-flash`, verified
  env + project `autoagents-500500`, wrote spec. Awaiting spec approval to start Phase 1.
- 2026-06-25 — Phases 1–4 (code) done. GCP setup, scaffold, brain built + smoke-tested,
  Firestore live, gateway code written. Installed terraform (direct binary) + resend CLI.
- 2026-06-25 — Provisioned Vertex AI Search datastore (transient 503, retried OK).
- 2026-06-25 — User pivot: vector store → **RAG Engine** + **Memory Bank** (both ADK-native).
  Created serverless RAG corpus in us-west1, wired tools + Memory Bank. RAG ingest+search
  verified live. Resend domain verified (sending); test email sent; key in Secret Manager.
  Next: deploy Agent Runtime (needs approval) + gateway + Resend inbound.
- 2026-06-25 — **Deployed.** Agent Runtime LIVE (engine 5931933951328256000) + Cloud Run gateway
  LIVE (public, approved). Webhook + signing secret + Cloud Scheduler set. MCP server built.
  End-to-end scheduler→gateway→Agent Runtime→Firestore loop verified. Only remaining: user
  enables Resend receiving + adds MX (Cloudflare) to unlock inbound email round-trip.
- 2026-06-25 — **Bugfix:** deployed brain's Firestore client used the project NUMBER
  (Agent Runtime sets GOOGLE_CLOUD_PROJECT to the number) → "database (default) does
  not exist"; `send_email` reported failure even though the email sent. Fix: coerce a
  numeric project → project ID in `app/config.py`; make `send_email` logging best-effort
  so it never masks a successful send. Redeployed brain.
- 2026-06-25 — **Correction (multi-tenant spike):** Memory Bank is wired in the agent
  (PreloadMemoryTool + after_agent_callback) but was **never enabled on the engine**
  (`contextSpec: False` = no memoryBankConfig). Long-term recall never actually worked;
  earlier "memory" was session reuse (short-term). Needs Phase 0.5 (enable memoryBankConfig)
  before per-user isolation can be validated. ADK `ToolContext.state` spike PASSED.
- 2026-06-25 — **Phase 0.5 DONE.** Enabled Memory Bank `context_spec` on the engine (genai
  SDK). `agents-cli deploy` strips context_spec, and a custom deploy hit build/source issues
  → chose to **orchestrate memory in the gateway** via the engine's working memory API
  (`async_search_memory` + `async_add_session_to_memory` in `query_agent`). Per-`user_id`,
  cross-session recall **verified** (stored "Lahore" → recalled in a fresh session). Real,
  isolated long-term memory now works for email + WhatsApp. Tenant-ready.
- 2026-06-25 — **WhatsApp channel LIVE** (Baileys bridge on e2-micro free VM). Inbound→agent→reply
  verified. Hit + fixed: AR-reader IAM for VM SA, live-QR rotation, post-pair 515, and a creds-backup
  flood that starved the e2-micro (now debounced + sequential). Note: `whatsapp-bridge-secret` was
  shown in chat via the `/qr` URL — rotate when convenient.
- 2026-06-25 — **Email channel COMPLETE.** Inbound MX live; text round-trip verified (Memory Bank
  stored a fact). Multimodal verified live: emailed image → agent fetched + described it.
  v1 done for email. Remaining optional: loop guard, daily digest, WhatsApp/calls (v2).
- 2026-06-25 — **Phase 1 (multi-tenant data model) DONE — additive, no behavior change.**
  Added registry collections `tenants`/`identities`/`threads` + `tenant_id` everywhere.
  New `gateway/tenancy.py` (normalize_email/phone, identity_key, resolve_tenant,
  create_tenant, add_identity) + collection constants in both configs. Migration
  `gateway/scripts/migrate_phase1.py` (idempotent): created **tenant_0** (owner emails +
  WhatsApp number), backfilled `tenant_id=tenant_0` on 60 messages + 11 tasks, copied
  `agent_state` singleton → per-tenant doc (singleton kept for back-compat). Verified
  `resolve_tenant(email/phone) → tenant_0`. Added 3 composite indexes
  (messages/tasks/threads by tenant_id, building async). Before threading the agent through
  this (Phase 3), only the registry exists — live single-tenant flows untouched.
- 2026-06-25 — **Phase 2 (gateway routing) DONE — first behavior change, deployed
  (rev autoagents-gateway-00007-27z, health 200, env/secrets preserved).** Inbound
  email + WhatsApp now resolve sender → tenant via `tenancy.resolve_tenant` (identity
  lookup). `user_id` for sessions + Memory Bank is now the **tenant_id** (per-tenant
  isolation), replacing raw email / `wa:<phone>`. New `_route_sender` → active / onboard /
  reject. **Unknown senders are now rejected** (logged `rejected_unknown`, no agent call).
  Pending tenants are **onboarded** on first message (flip to active + welcome, then
  process). Run-state (`!pause`/`!stop`/etc.) is now **per-tenant** (`agent_state/<tenant_id>`)
  and control commands are honoured only from the tenant's own identity. Scheduler still
  single-tenant (runs as tenant_0) until Phase 5. Routing verified live (owner→active,
  unknown→reject, pending→onboard→active, phone-normalization). NOTE: tenant_0's prior
  memories under the old email/`wa:` user_id are orphaned by the switch — agent re-learns.
- 2026-06-25 — **Phase 2 live-tested.** Email VERIFIED both ways: owner (`shahirshamim15314@`)
  → `received tenant_0` → replied; unregistered (`laibahiqbal96@`) → `rejected_unknown`,
  no agent call, no reply. **WhatsApp LID issue found:** the Baileys bridge delivers
  `m.key.remoteJid` which is now a rotating **LID** (`262439698465015@lid`,
  `88025757409510@lid`) — NOT the phone `923070251725`. So phone-based identity routing
  misses. Stopgap: registered both known LIDs → tenant_0. **KNOWN GAP / hardening task:**
  WhatsApp routing is brittle until the bridge resolves + sends the real phone number
  (`key.senderPn` / lid→PN mapping); required before onboarding NEW WhatsApp users by phone.
  Outbound sends still log with empty tenant_id (minor; tag later).
- 2026-06-25 — **Phase 3 (tenant-aware tools + per-tenant RAG) DONE — deployed.** Agent
  redeployed (Agent Runtime, same engine, ~4min) + gateway rev 00008-kzz. `app/tools.py`:
  every tool reads `tenant_id` (+ `rag_corpus`) from `ToolContext.state` (gateway injects
  it at session creation) and scopes all Firestore reads/writes + RAG to that tenant;
  `send_email` now sends from the reply-routable tag `assistant+<tenant_id>@jmkn.tech`;
  `cancel_task` refuses cross-tenant ids. **RAG decision changed:** locked "shared corpus +
  metadata filter" → **per-tenant corpus** (deployed `import_files` has no per-file metadata
  param, and separate corpora give physical isolation at no always-on cost); tenant_0 mapped
  to the existing corpus, stored on its tenant doc. Gateway `ensure_session(state=...)`
  injects `{tenant_id, rag_corpus}` and reuses a session only if it already carries that
  tenant's state (self-heals pre-multitenant sessions). Removed the now-redundant dormant
  Memory Bank wiring (`PreloadMemoryTool` + `after_agent_callback`) — also stops a prod
  background error. **Verified:** local Runner isolation PASS; **live two-tenant leak test
  PASS** (tenant B sees neither A's task nor A's documents; task-isolation + doc-routing).
