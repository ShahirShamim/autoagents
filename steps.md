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

## v2 (deferred)
- [ ] WhatsApp channel (decide official Cloud API vs unofficial)
- [ ] Voice calls (decide provider + budget)
- [ ] Optional web dashboard

---

## Documentation
- `docs/HUMAN_GUIDE.md` — novice walkthrough: concepts, prerequisites, step-by-step setup, parameters, operations, troubleshooting.
- `docs/AGENT_GUIDE.md` — machine-oriented: exact ordered commands, verification checks, resource inventory, env/secrets/IAM, pitfalls.

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
- 2026-06-25 — **Email channel COMPLETE.** Inbound MX live; text round-trip verified (Memory Bank
  stored a fact). Multimodal verified live: emailed image → agent fetched + described it.
  v1 done for email. Remaining optional: loop guard, daily digest, WhatsApp/calls (v2).
