# autoagents — Agent Reproduction & Operations Guide

Machine-oriented spec for an AI coding agent to **reproduce, operate, or extend**
this system. Terse, exact, ordered. Each step has a command and a verification.
Honor idempotency notes. Do not skip the PITFALLS section — they cost real cycles.

---

## 0. GOAL / INVARIANTS

- Build a messaging-driven autonomous assistant on GCP using Google ADK (`agents-cli`).
- Three deployables: **Agent Runtime** (ADK brain) + **Cloud Run gateway** (event layer)
  + **WhatsApp bridge** (Baileys, Node) on an always-on e2-micro VM.
- Channels: **email** (Resend) + **WhatsApp** (Baileys, unofficial). Voice calls = deferred.
- Model = `gemini-3.5-flash` (multimodal). Do NOT change unless instructed.
- Budget posture: GCP $300 trial; avoid always-on resources except the e2-micro
  free-tier VM the WhatsApp bridge needs (no Vertex Vector Search, no Cloud Run min-1).

## 1. IDENTIFIERS (this deployment)

```
PROJECT_ID        = autoagents-500500
PROJECT_NUMBER    = 323512451403
REGION            = us-central1
RAG_LOCATION      = us-west1            # us-central1 RAG restricted for new projects
MODEL             = gemini-3.5-flash    # model location = global
AUTH_ACCOUNT      = jmkntech@gmail.com  # holds $300 credit
AGENT_ENGINE      = projects/323512451403/locations/us-central1/reasoningEngines/5931933951328256000
RAG_CORPUS        = projects/323512451403/locations/us-west1/ragCorpora/4611686018427387904
GATEWAY_URL       = https://autoagents-gateway-323512451403.us-central1.run.app
SENDER_EMAIL      = assistant@jmkn.tech
RESEND_DOMAIN_ID  = c4f6f08e-76ff-4db8-85bd-74b938b18cea  (jmkn.tech)
RESEND_WEBHOOK_ID = 7f422451-e861-4013-9821-b1a825df760b  (email.received)
RUNTIME_SA        = service-323512451403@gcp-sa-aiplatform-re.iam.gserviceaccount.com
GATEWAY_SA        = autoagents-gateway@autoagents-500500.iam.gserviceaccount.com
ATTACH_BUCKET     = autoagents-500500-attachments
DOCS_BUCKET       = autoagents-500500-autoagents-agent-docs   # leftover from Vertex AI Search
WA_VM             = autoagents-wa (e2-micro, us-central1-a)
WA_IP             = 136.114.229.113   (static, port 8080)
WA_IMAGE          = us-central1-docker.pkg.dev/autoagents-500500/autoagents/whatsapp-bridge:latest
WA_NUMBER         = +44 7340 926493   (dedicated, linked via Baileys)
WA_AUTH_GCS       = gs://autoagents-500500-attachments/wa-auth/
AR_REPO           = us-central1-docker.pkg.dev/autoagents-500500/autoagents
```

## 2. TOOLCHAIN (preconditions)

```
uv            -> uv tool install google-agents-cli         # provides `agents-cli`
gcloud        -> authed (gcloud auth list), ADC quota project set to PROJECT_ID
terraform     -> v1.x via prebuilt binary to ~/.local/bin  (brew + tap unreliable)
resend-cli    -> npm install -g resend-cli ; `resend login`
node/npm      -> required by resend-cli
```
Verify: `agents-cli info`; `gcloud config get-value project`; `terraform version`;
`resend --version`.

## 3. ORDERED BUILD

### 3.1 GCP base
```bash
gcloud config set project PROJECT_ID
gcloud services enable aiplatform.googleapis.com run.googleapis.com \
  cloudscheduler.googleapis.com firestore.googleapis.com storage.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com iam.googleapis.com
gcloud firestore databases create --location=us-central1          # idempotent-ish; errors if exists
gcloud storage buckets create gs://ATTACH_BUCKET --location=us-central1 --uniform-bucket-level-access
gcloud auth application-default set-quota-project PROJECT_ID
```
VERIFY: `gcloud firestore databases list`; `gcloud storage buckets list`.

### 3.2 Scaffold (prototype-first)
```bash
agents-cli scaffold create autoagents-agent \
  --agent agentic_rag --datastore agent_platform_search \
  --deployment-target agent_runtime --region us-central1 \
  --agent-guidance-filename CLAUDE.md --prototype --auto-approve
cd autoagents-agent && agents-cli install
```
VERIFY: `agents-cli info` shows `base_template: agentic_rag`, `deployment_target: agent_runtime`.
NOTE: `--prototype` still emits `deployment/terraform/single-project` for the datastore.

### 3.3 RAG Engine corpus (Serverless)
```bash
uv run python scripts/setup_rag_corpus.py   # sets engine config tier=Basic, creates corpus in us-west1
```
Logic if writing fresh:
```python
from vertexai import rag
from vertexai.rag.utils import resources as r
rag.update_rag_engine_config(rag_engine_config=rag.RagEngineConfig(
    name=f"projects/{P}/locations/us-central1/ragEngineConfig",
    rag_managed_db_config=rag.RagManagedDbConfig(tier=r.Basic())))
corpus = rag.create_corpus(display_name="autoagents-docs")   # init location MUST be us-west1
```
VERIFY: `rag.list_corpora()` returns the corpus; `search_documents()` returns contexts after an ingest.

### 3.4 Agent code (app/)
- `app/config.py`: PROJECT_ID, REGION, LLM=`gemini-3.5-flash`, ATTACHMENTS_BUCKET,
  SENDER_EMAIL, RESEND_API_KEY (env), RAG_LOCATION, RAG_CORPUS, ADMIN_EMAILS,
  Firestore collection names. ADMIN_EMAILS parser MUST split on `[;,]` (see PITFALLS).
- `app/tools.py`: typed function tools, each with a docstring (ADK derives schema):
  `send_email, schedule_task, list_tasks, cancel_task, query_messages,
   get_agent_state, set_agent_state, current_time, search_documents, ingest_document`.
  Firestore client lazy-init; `_now()` = UTC ISO. RAG tools lazy `vertexai.init(location=RAG_LOCATION)`.
- `app/agent.py`: model `gemini-3.5-flash`; tools list = the 10 above + `PreloadMemoryTool()`;
  `after_agent_callback=generate_memories_callback` where callback awaits
  `callback_context.add_session_to_memory()`. Imports:
  `from google.adk.tools.preload_memory_tool import PreloadMemoryTool`,
  `from google.adk.agents.callback_context import CallbackContext`.
- `app/mcp_server.py`: FastMCP registering the same tools (`uv sync --extra mcp`).
- `.env`: GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION=global, GOOGLE_GENAI_USE_VERTEXAI=True,
  ATTACHMENTS_BUCKET, SENDER_EMAIL, ADMIN_EMAILS, RESEND_API_KEY, RAG_LOCATION, RAG_CORPUS.
VERIFY: `agents-cli run "use current_time and tell me the time"` → tool call + text.
VERIFY (Firestore): `agents-cli run "schedule a reminder for 2099-01-01T00:00:00+00:00"` → writes `tasks`.

### 3.5 Firestore composite indexes
```bash
gcloud firestore indexes composite create --collection-group=tasks \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=due_at,order=ascending
gcloud firestore indexes composite create --collection-group=messages \
  --field-config=field-path=channel,order=ascending \
  --field-config=field-path=ts,order=descending
```
Mirror in `firestore.indexes.json`. VERIFY: `gcloud firestore indexes composite list` → STATE READY.

### 3.6 Resend (sending) + secret
```bash
resend login                                   # stores API key locally + in .env
resend domains list                            # jmkn.tech status=verified, sending=enabled
printf '%s' "$RESEND_API_KEY" | gcloud secrets create resend-api-key --data-file=-
```
VERIFY: `tools.send_email(<your addr>, ...)` returns {"ok": true, "id": ...}.

### 3.7 Deploy brain (Agent Runtime)
```bash
RT="serviceAccount:service-323512451403@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
for r in datastore.user aiplatform.user storage.objectAdmin secretmanager.secretAccessor logging.logWriter; do
  gcloud projects add-iam-policy-binding PROJECT_ID --member="$RT" --role="roles/$r" --condition=None; done

agents-cli deploy --project PROJECT_ID --region us-central1 --no-confirm-project --no-wait \
  --secrets "RESEND_API_KEY=resend-api-key" \
  --update-env-vars "RAG_CORPUS=<corpus>,RAG_LOCATION=us-west1,ATTACHMENTS_BUCKET=ATTACH_BUCKET,SENDER_EMAIL=assistant@jmkn.tech"
# poll:
agents-cli deploy --status      # repeat until "Deployment successful"; prints reasoningEngines/<id>
```
NOTE: do NOT pass ADMIN_EMAILS here (comma issue; gateway enforces admin, brain doesn't).
VERIFY (SDK):
```python
from vertexai import agent_engines; import vertexai
vertexai.init(project=P, location="us-central1")
e=agent_engines.get(AGENT_ENGINE); s=e.create_session(user_id="t")
list(e.stream_query(user_id="t", session_id=s["id"], message="hi"))  # yields events; text in content.parts[].text
```
Multimodal message form: `{"role":"user","parts":[{"text":...},{"file_data":{"file_uri":"gs://...","mime_type":...}}]}`.

### 3.8 Deploy gateway (Cloud Run)
```bash
gcloud iam service-accounts create autoagents-gateway
G="serviceAccount:GATEWAY_SA"
for r in aiplatform.user datastore.user storage.objectAdmin secretmanager.secretAccessor logging.logWriter; do
  gcloud projects add-iam-policy-binding PROJECT_ID --member="$G" --role="roles/$r" --condition=None; done
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create tasks-token --data-file=-

gcloud run deploy autoagents-gateway --source gateway/ --region us-central1 \
  --service-account GATEWAY_SA --allow-unauthenticated --memory 1Gi \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=PROJECT_ID,GOOGLE_CLOUD_LOCATION=us-central1,AGENT_ENGINE_RESOURCE=<engine>,ATTACHMENTS_BUCKET=ATTACH_BUCKET,SENDER_EMAIL=assistant@jmkn.tech,ADMIN_EMAILS=you@gmail.com;other@gmail.com" \
  --set-secrets "RESEND_API_KEY=resend-api-key:latest,TASKS_TOKEN=tasks-token:latest"
```
NOTE: `--allow-unauthenticated` triggers a human approval gate in some harnesses
(public endpoint). It IS required for the Resend webhook. On REDEPLOY, omit the flag
(IAM unchanged) to avoid re-prompting.
VERIFY: `curl $GATEWAY_URL/health` → `{"status":"ok"}`; `curl -X POST $GATEWAY_URL/tasks/run` → 401.

### 3.9 Resend inbound + scheduler
```bash
# webhook -> capture signing_secret (shown once)
resend webhooks create --endpoint $GATEWAY_URL/inbound/email --events email.received --json
printf '%s' "<signing_secret>" | gcloud secrets create resend-webhook-secret --data-file=-
# redeploy gateway adding RESEND_WEBHOOK_SECRET=resend-webhook-secret:latest to --set-secrets

# enable receiving (CLI lacks a flag; use REST)
curl -X PATCH "https://api.resend.com/domains/RESEND_DOMAIN_ID" \
  -H "Authorization: Bearer $RESEND_API_KEY" -H "Content-Type: application/json" -d '{"receiving":true}'
# DNS (user): MX @ -> inbound-smtp.us-east-1.amazonaws.com priority 10

# scheduler
TOKEN=$(gcloud secrets versions access latest --secret=tasks-token)
gcloud scheduler jobs create http autoagents-tasks-tick --location=us-central1 \
  --schedule="*/5 * * * *" --http-method=POST --uri="$GATEWAY_URL/tasks/run" \
  --headers="X-Tasks-Token=${TOKEN}" --attempt-deadline=180s
```
VERIFY inbound: send external email -> Firestore `messages` gets direction=in + an out reply.
VERIFY scheduler: schedule a past-due task; `curl -X POST -H "X-Tasks-Token: $TOKEN" $GATEWAY_URL/tasks/run` -> {"ran":1}; task status=done.

### 3.10 WhatsApp bridge (Baileys, e2-micro VM) — optional channel
```bash
gcloud services enable compute.googleapis.com
gcloud artifacts repositories create autoagents --repository-format=docker --location=us-central1
gcloud builds submit --tag WA_IMAGE whatsapp-bridge/
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create whatsapp-bridge-secret --data-file=-
gcloud compute addresses create autoagents-wa-ip --region=us-central1
gcloud compute firewall-rules create allow-wa-bridge --direction=INGRESS --action=ALLOW \
  --rules=tcp:8080 --target-tags=wa-bridge --source-ranges=0.0.0.0/0
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:GATEWAY_SA" --role="roles/artifactregistry.reader"   # else container can't pull
WA=$(gcloud secrets versions access latest --secret=whatsapp-bridge-secret)
gcloud compute instances create-with-container autoagents-wa --zone=us-central1-a \
  --machine-type=e2-micro --container-image=WA_IMAGE \
  --container-env=GCS_BUCKET=ATTACH_BUCKET,WA_AUTH_PREFIX=wa-auth/,GATEWAY_INBOUND_URL=$GATEWAY_URL/inbound/whatsapp,WA_SECRET=$WA,AUTH_DIR=/data/auth,PORT=8080 \
  --service-account=GATEWAY_SA --scopes=cloud-platform \
  --address=WA_IP --tags=wa-bridge --boot-disk-size=10GB
```
Then redeploy gateway + agent adding env `WHATSAPP_BRIDGE_URL=http://WA_IP:8080` and secret
`WHATSAPP_BRIDGE_SECRET=whatsapp-bridge-secret(:latest)` (agent `send_whatsapp` needs both).
PAIR: open `http://WA_IP:8080/qr?token=$WA` (live, auto-refresh) → scan with WA_NUMBER →
Linked Devices → Link a Device. Creds persist to WA_AUTH_GCS; restart reconnects without QR.
VERIFY: `curl http://WA_IP:8080/health` → `connected:true`; message the number → Firestore
`messages` channel=whatsapp (in + out); reply delivered. Update code → rebuild+push → `gcloud compute instances reset autoagents-wa --zone=us-central1-a`.

## 4. GATEWAY CONTRACT (`gateway/`)

- `POST /inbound/email`: verify Svix sig (RESEND_WEBHOOK_SECRET) → parse → resolve
  `email_id` (webhook `data.id`/`email_id`, else `latest_inbound_id()`) → fetch full
  email (`GET /emails/receiving/{id}`) for body → list attachments
  (`GET /emails/receiving/{id}/attachments`, each has `download_url`) → download →
  GCS → build `file_data` parts for model-supported mimes → log inbound →
  check `agent_state` → admin-command short-circuit → `query_agent(...)` → `send_email` reply → log out.
- `POST /tasks/run`: require header `X-Tasks-Token == TASKS_TOKEN`; if state==running,
  run due tasks (Firestore `due_at <= now`, status=pending) via `query_agent`; mark done/error.
- `GET /health`: liveness. (Do NOT use `/healthz` — Google edge intercepts it.)
- `query_agent(user_id, session_id, message, files=None)`: if files, send
  `{"role":"user","parts":[{"text":...}, {"file_data":{file_uri,mime_type}}...]}` else plain string.
- Model-supported mimes: `application/pdf` or prefix `image/ audio/ video/ text/`.
- `POST /inbound/whatsapp`: require `X-WA-Secret == WHATSAPP_BRIDGE_SECRET`; body
  `{from, text, media:{uri,type}|null, name}`. Log (channel=whatsapp) → admin (sender in
  ADMIN_WHATSAPP and text starts "!") → state → media→file_data → `query_agent(user_id="wa:"+from)` →
  `send_whatsapp` reply. `@lid` jids pass through verbatim.
- WhatsApp send: `clients.send_whatsapp(to, text)` → `POST http://WA_IP:8080/send` (X-WA-Secret).
  The agent's `send_whatsapp` tool calls the same bridge directly.
- Bridge `whatsapp-bridge/index.js` (Node/Baileys): socket + QR via connection.update; auth via
  `useMultiFileAuthState` restored-from / backed-up-to GCS (DEBOUNCED, sequential, resumable:false);
  inbound→GATEWAY_INBOUND_URL; media→GCS; HTTP `/qr` (token), `/send` (secret), `/health`. DMs only.

## 5. RESOURCE INVENTORY

| Kind | Name |
|------|------|
| Reasoning Engine | reasoningEngines/5931933951328256000 |
| Cloud Run | autoagents-gateway (us-central1, public) |
| Firestore | (default), Native, us-central1; collections messages/tasks/agent_state/contacts |
| Indexes | tasks(status,due_at); messages(channel,ts desc) |
| GCS | autoagents-500500-attachments; autoagents-500500-autoagents-agent-docs |
| RAG corpus | ragCorpora/4611686018427387904 (us-west1, Basic) |
| Secrets | resend-api-key, resend-webhook-secret, tasks-token |
| Service accts | autoagents-gateway@…; runtime service agent gcp-sa-aiplatform-re |
| Scheduler | autoagents-tasks-tick (*/5 * * * *) |
| Resend | domain jmkn.tech (send+recv); webhook email.received |
| Compute VM | autoagents-wa (e2-micro, us-central1-a), static IP 136.114.229.113, tag wa-bridge |
| Firewall | allow-wa-bridge (INGRESS tcp:8080, 0.0.0.0/0) |
| Artifact Registry | repo `autoagents` (us-central1); image whatsapp-bridge:latest |
| WhatsApp | Baileys, dedicated number +44 7340 926493; auth in gs://…/wa-auth/ |

## 6. ENV / SECRETS

Brain (Agent Runtime): RAG_CORPUS, RAG_LOCATION, ATTACHMENTS_BUCKET, SENDER_EMAIL,
WHATSAPP_BRIDGE_URL; secrets RESEND_API_KEY, WHATSAPP_BRIDGE_SECRET.
(GOOGLE_CLOUD_PROJECT/LOCATION set in code: project from ADC, location=global.)
Gateway (Cloud Run): GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION=us-central1,
AGENT_ENGINE_RESOURCE, ATTACHMENTS_BUCKET, SENDER_EMAIL, ADMIN_EMAILS(`;`-sep),
WHATSAPP_BRIDGE_URL, ADMIN_WHATSAPP(`;`-sep, optional);
secrets RESEND_API_KEY, RESEND_WEBHOOK_SECRET, TASKS_TOKEN, WHATSAPP_BRIDGE_SECRET.
Bridge (VM container env): GCS_BUCKET, WA_AUTH_PREFIX, GATEWAY_INBOUND_URL, WA_SECRET,
AUTH_DIR, PORT.

## 7. PITFALLS (verified; do not rediscover)

1. **Terraform install**: `brew install terraform` is a no-op; tap builds from source
   (needs Xcode CLT). Use the prebuilt binary into `~/.local/bin`.
2. **RAG Spanner restriction**: new projects can't create Spanner-mode RAG corpora in
   us-central1/us-east1/us-east4. Set engine `tier=Basic` AND create corpus in `us-west1`.
   `create_corpus` with explicit `RagManagedDb()` backend errors ("Unknown field CopyFrom").
3. **Datastore 503**: discoveryengine transient 503 mid-apply; re-run `agents-cli infra datastore`.
4. **`/healthz`**: Google edge returns its own 404 (no `server: Google Frontend` header);
   request never reaches the container. Use `/health`.
5. **Resend webhook is metadata-only-ish**: body sometimes present in payload, but rely on
   `GET /emails/receiving/{id}`. Attachments are NOT inline — list endpoint yields `download_url`.
6. **Webhook `email_id`**: not reliably in the payload; fall back to `latest_inbound_id()`.
7. **Env var commas**: Cloud Run/Agent Runtime split `--set-env-vars`/`--update-env-vars`
   on commas. Multi-value (ADMIN_EMAILS) must use `;` and code splits on `[;,]`.
8. **`--allow-unauthenticated`**: required (Resend webhook is unauthenticated); may hit a
   human-approval gate. Protect endpoints in-app (Svix sig + TASKS_TOKEN). Omit flag on redeploy.
9. **IAM for runtime**: deployed agent's tools need the runtime SA to have
   datastore.user + aiplatform.user + storage.objectAdmin + secretAccessor. Grant before/after deploy.
10. **Agent Runtime deploy time**: 5-10 min; use `--no-wait` then poll `--status` (avoids timeouts).
11. **Cross-region RAG**: corpus in us-west1, model in global, gateway/engine in us-central1 — all fine,
    reference resources by full name; rag tools `vertexai.init(location=us-west1)`.
12. **Agent Runtime project = NUMBER**: on Agent Runtime, `google.auth.default()` /
    `GOOGLE_CLOUD_PROJECT` is the project NUMBER. The Firestore data API then 404s
    ("database (default) does not exist for project <number>"). Coerce numeric project →
    project ID before constructing `firestore.Client`. Local runs use the string ID, so
    this only surfaces post-deploy.
13. **Logging must be best-effort**: never let a Firestore log write fail an external
    side effect. `send_email` sends via Resend THEN logs — wrap the log in try/except so a
    logging failure doesn't return ok=False after the email already went out.
14. **WhatsApp = Baileys (unofficial)**: against WhatsApp ToS (ban risk) → DEDICATED number.
    Needs an always-on socket → e2-micro VM, not Cloud Run scale-to-zero.
15. **VM SA needs `roles/artifactregistry.reader`** or konlet can't pull the image
    ("downloadArtifacts denied"). Diagnose via `get-serial-port-output`; grant then `reset`.
16. **QR rotates (~20-30s)**: a static screenshot fails to link ("couldn't link, try again").
    Serve a LIVE auto-refreshing `/qr` page; scan the on-screen QR. Repeated failures trigger a
    WhatsApp throttle — wait minutes.
17. **Post-pair close code 515** = restartRequired (normal); reconnect from saved creds, don't re-QR.
18. **Baileys `creds.update` storms** (fires per pre-key): backing up the whole auth dir in
    parallel on every event floods the 1GB e2-micro → socket-hangups + HTTP flaps. DEBOUNCE
    (single timer), upload SEQUENTIALLY with `resumable:false`. Persist to GCS so restarts
    reconnect (creds.json alone re-auths; pre-keys re-sync).
19. **`@lid` addressing**: newer WhatsApp identifies senders by a LID (`<id>@lid`), not the phone
    number. Pass the jid through verbatim for replies; don't assume `@s.whatsapp.net`.
20. **`create-with-container` deprecation**: emits a warning (container-VM startup agent being
    discontinued). Works today; long-term migrate to a startup-script-run container or MIG.

## 8. OPERATIONS

- Admin (allow-listed email): `!status`, `!pause`, `!resume`, `!stop`.
- Logs: Cloud Run service logs (gateway); Cloud Logging
  `resource.type=aiplatform.googleapis.com/ReasoningEngine` (brain).
- Redeploy brain: `agents-cli deploy ...` (no revision rollback; fix+redeploy).
- Redeploy gateway: `gcloud run deploy autoagents-gateway --source gateway/ ...` (omit allow-unauth).
- Rotate a secret: `gcloud secrets versions add <name> --data-file=-`, then redeploy.
- WhatsApp bridge: health `curl http://WA_IP:8080/health`; logs Cloud Logging
  `logName=~"cos_containers"`; restart `gcloud compute instances reset autoagents-wa --zone=us-central1-a`;
  update code → rebuild+push image → reset. Re-pair only if creds lost: open `/qr?token=$WA`.
- Rotate `whatsapp-bridge-secret`: add a new version → recreate VM with new `WA_SECRET`
  container-env → redeploy gateway+agent. (It was exposed in chat via the `/qr` URL during pairing.)

## 9. DEFERRED (v2)

Voice calls (provider/budget), WhatsApp groups + WA admin (DMs only now), loop-guard
(ignore sender==SENDER_EMAIL), daily-digest scheduler job, observability dashboards,
full inline-bytes multimodal (currently GCS file_data, which is sufficient),
rotate whatsapp-bridge-secret.
```

---

## 10. MULTI-TENANT ARCHITECTURE (Phases 0–7, all live)

**Model.** One Agent Runtime engine + one gateway + one admin webapp serve N tenants.
Isolation key = `tenant_id`, used as the Agent Runtime `user_id` (so sessions + Memory
Bank isolate per tenant) and as a filter on all Firestore writes/reads + per-tenant RAG corpus.

**Firestore collections (added).**
- `tenants/{id}`: `{name, status: pending|active|disabled, emails[], phones[], rag_corpus, ...}`
- `identities/{email:<addr>|phone:<digits>}`: `{tenant_id, channel}` — point-lookup routing.
- `threads/{tenant:channel:contact}`: third-party reply window `{first_reply_at, expires_at,
  status, closed_notified}`.
- `tenant_id` field added to `messages`, `tasks`; `agent_state` keyed by `tenant_id`.

**Routing (gateway `_route_sender`).** identity → (tagged-address thread) → reject.
`resolve_tenant(channel, sender)`; pending → onboard (activate + welcome + `ensure_tenant_corpus`);
unknown → `rejected_unknown`, no agent call. `user_id = tenant_id` everywhere.

**Tenant-aware tools (`app/tools.py`).** Each tool takes `tool_context: ToolContext` and reads
`tool_context.state["tenant_id"]` / `["rag_corpus"]` (gateway injects via
`ensure_session(state=...)` at session creation). Fallback = `DEFAULT_TENANT` (never an unscoped
query). `send_email` sends from `assistant+<tenant>@jmkn.tech` (reply-routable tag).

**RAG = per-tenant corpus** (NOT shared+filter — deployed `import_files` has no per-file
metadata; separate corpora give physical isolation, no always-on cost). `ensure_tenant_corpus`
creates one at onboarding (us-west1, Basic tier).

**Third-party threads + 3h TTL (gateway-only).** Outbound from the tagged address + the
outbound `messages` log let the gateway correlate replies (`parse_tagged_tenant` +
`latest_outbound_to`). `apply_thread_ttl` = window state machine (first reply opens 3h; expired
→ block + one courtesy note; a newer outbound reopens). Reply → agent summary → relayed to
tenant owner (`primary_email`).

**Admin webapp** = separate Cloud Run service `admin/`, private (`--no-allow-unauthenticated`,
reached via `gcloud run services proxy`), shared-password signed cookie (`COOKIE_SECURE` env-gated
because the proxy is http://localhost).

**Scheduler** `/tasks/run` runs each due task under its own `tenant_id`; skips paused/stopped tenants.

### PITFALLS (multi-tenant; verified)
- **`asyncio.run()` inside the async FastAPI handlers throws** ("cannot be called from a running
  event loop") → the gateway's Memory Bank calls **silently failed in production** (only the sync
  test path ever worked). Fix: `clients._run_async` runs the coroutine in a worker thread when a
  loop is active. Any future engine-async call from a handler MUST go through it.
- **Engine exposes only `async_*` memory methods** (`async_search_memory`,
  `async_add_session_to_memory`) — no sync variants.
- **Session state is fixed at creation.** `ensure_session` reuses a session only if its state's
  `tenant_id` matches; else creates a fresh stateful one (self-heals pre-multitenant sessions).
- **WhatsApp inbound is a rotating LID**, not the phone → third-party WhatsApp threads + phone-based
  onboarding don't correlate. Register the current LID as a stopgap; real fix = bridge sends `senderPn`.
- **IAM propagation lag** can make a just-granted role (e.g. `aiplatform.user` for corpus create)
  fail for ~1–2 min; `ensure_tenant_corpus` logs + degrades, backfill via `/internal/ensure-corpus/{tid}`.
- **`agents-cli deploy` strips `context_spec`** → native Memory Bank can't be wired on the agent;
  memory is orchestrated in the gateway instead.

### PITFALL — Agent Runtime ignores `.env`
The deployed agent runs on `app/config.py` defaults + the engine's `env` / `secretEnv`, NOT
the repo `.env` (that's only for local `agents-cli run`/`playground`). Set/inspect runtime
config via `agents-cli deploy --update-env-vars KEY=VAL --secrets ENV=secret-name` (values
persist across redeploys). Inspect the live engine with the REST API
`GET https://us-central1-aiplatform.googleapis.com/v1/projects/<proj>/locations/us-central1/reasoningEngines/<id>`
→ `spec.deploymentSpec.env` / `secretEnv` (there is no `gcloud ai reasoning-engines` command).
Example bug: WhatsApp sends failed because `WHATSAPP_BRIDGE_URL` was never set on the engine
(the secret was) — editing `.env` did nothing.

### ANALYTICS (per-tenant token usage)
- **Capture (gateway `query_agent`):** sum `usage_metadata` across every event in a turn
  (tool steps = multiple LLM calls, each with its own metadata) → `clients.record_usage` writes
  `usage/{id} = {tenant_id, model, prompt_tokens, output_tokens, thoughts_tokens, total_tokens, ts}`.
  Best-effort; never blocks the reply. Meters forward only (no backfill).
- **Admin aggregation (`admin/tenancy.py`):** `tenant_usage(tid)` sums a tenant's records;
  `all_usage()` is one pass → per-tenant + grand totals (index roll-up); `_count()` uses Firestore
  `count()` aggregation (stream fallback) for message/task counts.
- **Cost (`admin/config.py`):** `LLM_INPUT_COST_PER_1M` / `LLM_OUTPUT_COST_PER_1M` (USD/1M tokens).
  Both 0 → tokens-only (no $). Cost = in_rate·prompt + out_rate·(output+thoughts); thinking tokens
  bill as output. Set on the admin service (`gcloud run services update … --update-env-vars`).
- Index `usage(tenant_id, ts)` exists for future time-series queries; the admin's reads don't
  order_by, so they need no composite index.

### SESSION LIFECYCLE (idle rotation)
- `ensure_session(user_id, state)` is the single entry point (called by every handler +
  `/tasks/run`). Source of truth = Firestore pointer `agent_sessions/<user_id> =
  {session_id, last_at}` (point lookup, no index).
- **Reuse** if `now - last_at <= SESSION_IDLE_HOURS` (config, default 8) → bump `last_at`.
- **Rotate** when idle exceeds the window: call `_store_memory(old)` FIRST; rotate (create a
  new stateful session, repoint) only if it returns True. If the flush fails, keep the old
  session and just bump `last_at` — memory is never dropped to start a new session.
- **First run / no pointer:** adopt the tenant's latest state-matching session via
  `_latest_matching_session` (graceful, no history reset), else create.
- Memory is still flushed after EVERY turn in `query_agent` (`_store_memory`, now returns bool);
  the rotation flush is the extra guarantee at the boundary.
- Why: caps unbounded session history (the main prompt-token cost driver — see Analytics) and
  removes reliance on `list_sessions()[0]` ordering.
