# autoagents — Human Setup & Operations Guide

A complete, novice-friendly guide to the autonomous email assistant we built on
Google Cloud. It explains **what** each piece is, **why** it's there, **how** to
set it up, the **parameters** we used, and how to **run and troubleshoot** it.

If you've never touched Google Cloud, you can still follow this top to bottom.

---

## 1. What is this?

**autoagents** is an autonomous assistant you talk to **by email and WhatsApp**.
You message it, it reads what you sent (including images, PDFs, audio, and video
attachments), thinks using Google's Gemini model, and replies on the same channel.
It remembers facts about you across conversations, can search documents you've
given it, can send emails and WhatsApp messages on your behalf, and can schedule
reminders/follow-ups that it carries out later.

### The big picture (architecture)

```
                ┌─────────────────────────────────────────────────────┐
   You (Gmail)  │                  Google Cloud (project autoagents)    │
       │        │                                                       │
       │ email  │   ┌──────────────┐         ┌────────────────────────┐ │
       └───────▶│   │ Cloud Run    │ query   │  Agent Runtime         │ │
   assistant@   │   │ "gateway"    │────────▶│  (the "brain", ADK)    │ │
   jmkn.tech    │   │ (FastAPI)    │◀────────│  model: gemini-3.5-flash│ │
       ▲        │   │              │ reply   │  + Memory Bank          │ │
       │ reply  │   │ /inbound/email         │  + Sessions             │ │
       └────────│   │ /tasks/run   │         └──────────┬─────────────┘ │
                │   │ /health      │                    │ tools          │
                │   └──────┬───────┘                    ▼                │
                │          │              ┌──────────────────────────┐   │
   Resend ──────┼──────────┘              │ Firestore (logs, tasks,  │   │
   (email in/out)│  webhook               │   state) · GCS (files) · │   │
                │  Cloud Scheduler ──────▶│   RAG Engine (doc search)│   │
                │  (every 5 min)          └──────────────────────────┘   │
                └─────────────────────────────────────────────────────┘
```

Three deployed services:

1. **Agent Runtime** — the "brain." A managed Google service that runs the agent
   logic (built with Google's **Agent Development Kit / ADK**). It can't receive
   webhooks, so it doesn't talk to the internet directly.
2. **Cloud Run gateway** — a tiny web service that **receives** inbound email
   (via a Resend webhook), inbound **WhatsApp** (via the bridge), and **scheduler
   ticks**, calls the brain, and sends replies. It's the public-facing part.
3. **WhatsApp bridge** — a small Node service (using **Baileys**, the unofficial
   WhatsApp Web library) that keeps a live WhatsApp connection. It runs on an
   always-on **e2-micro free VM** because Baileys needs a persistent socket
   (it can't scale to zero). Inbound WhatsApp → gateway; the gateway/agent → bridge
   to send. Auth session is saved to Cloud Storage so restarts don't need re-pairing.

Supporting pieces: **Firestore** (database for logs/tasks/state), **Cloud Storage**
(stores attachments), **RAG Engine** (document search / "long-term document memory"),
**Memory Bank** (remembers facts about you), **Cloud Scheduler** (runs due tasks),
**Secret Manager** (API keys), and **Resend** (sends and receives email).

---

## 2. What you need (prerequisites)

### Accounts
- A **Google Cloud** account with a project. Ours is named `autoagents`
  (project ID `autoagents-500500`). New accounts get **$300 free credit / 90 days** —
  plenty for this.
- A **Resend** account (free tier) for sending/receiving email.
- A **domain** with DNS you control. Ours is `jmkn.tech` on **Cloudflare**.

### Local tools (install once)
| Tool | What it is | Install |
|------|-----------|---------|
| `gcloud` | Google Cloud command line | https://cloud.google.com/sdk/docs/install |
| `uv` | Fast Python package manager | https://docs.astral.sh/uv/getting-started/installation/ |
| `agents-cli` | Google ADK toolkit (the "Agents CLI") | `uv tool install google-agents-cli` |
| `terraform` | Infrastructure tool (used by agents-cli) | **Download the binary** (see note) |
| `resend` | Resend command line | `npm install -g resend-cli` |
| `node` / `npm` | Needed for the Resend CLI | https://nodejs.org |

> **Terraform note:** `brew install terraform` no longer works (license change) and
> the HashiCorp tap builds from source (needs Xcode tools). The reliable way on macOS
> is to download the prebuilt binary:
> ```bash
> VER=$(curl -s https://api.releases.hashicorp.com/v1/releases/terraform/latest | python3 -c "import sys,json;print(json.load(sys.stdin)['version'])")
> curl -sLo /tmp/tf.zip "https://releases.hashicorp.com/terraform/${VER}/terraform_${VER}_darwin_arm64.zip"
> mkdir -p ~/.local/bin && unzip -o /tmp/tf.zip -d ~/.local/bin
> terraform version   # we used v1.15.7
> ```

### Costs (what actually bills)
- Everything fits inside the **$300 GCP trial**. Idle services (Agent Runtime,
  Cloud Run with scale-to-zero) **don't bill when not in use**.
- **Avoid Vertex AI Vector Search** — its index endpoint runs 24/7 (~$150/month).
  We used **RAG Engine (serverless)** instead, which is pay-per-use and cheap.
- **Resend free tier** = 1 domain, generous sending. Inbound receiving is included.
- Gemini 3.5 Flash pricing is ~$1.50 per million input tokens, ~$9 per million output —
  trivial for personal use.

---

## 3. The key decisions we made (and why)

| Decision | Choice | Why |
|----------|--------|-----|
| Model | `gemini-3.5-flash` | Fast, cheap, multimodal (reads images/PDF/audio/video) |
| Where the brain runs | **Agent Runtime** | Managed, idle = free, native Sessions + Memory Bank |
| How email arrives | **Cloud Run gateway** | Agent Runtime can't receive webhooks |
| Long-term *document* store | **RAG Engine** (serverless) | ADK-native, cheap, simple "upload file → searchable" |
| Long-term *personal* memory | **Memory Bank** | Managed; auto-extracts facts about you |
| Short-term memory | **Agent Runtime Sessions** | Native, per-conversation |
| Database | **Firestore (Native)** | Free tier, serverless, easy |
| Email provider | **Resend** | Simple API + CLI, free tier, inbound support |
| Region | `us-central1` (RAG corpus in `us-west1`) | Broad support; RAG `us-central1` was capacity-restricted for new projects |
| WhatsApp method | **Baileys** (unofficial WhatsApp Web) | Free, links *your* number, no Meta verification. ToS/ban risk → use a dedicated number. |
| WhatsApp hosting | **e2-micro free VM** | Baileys needs an always-on socket (can't scale to zero); the always-free VM keeps it ~$0. |

---

## 4. Step-by-step setup

> All commands assume you've installed the tools above and run
> `gcloud auth login` and `agents-cli login -i` once.

### Phase 1 — Google Cloud setup

```bash
# Point gcloud at your project
gcloud config set project autoagents-500500

# Make sure billing is on (should print billingEnabled: true)
gcloud billing projects describe autoagents-500500

# Turn on the APIs we use
gcloud services enable \
  aiplatform.googleapis.com run.googleapis.com cloudscheduler.googleapis.com \
  firestore.googleapis.com storage.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com iam.googleapis.com

# Create the database (Firestore in "Native" mode), in us-central1
gcloud firestore databases create --location=us-central1

# Create a bucket to hold email attachments
gcloud storage buckets create gs://autoagents-500500-attachments \
  --location=us-central1 --uniform-bucket-level-access

# Fix a credentials warning so libraries bill the right project
gcloud auth application-default set-quota-project autoagents-500500
```

**What just happened:** you enabled the services, created a database, and a file
store. None of this costs anything meaningful.

### Phase 2 — Create the agent project (scaffold)

The `agents-cli` tool generates a ready-to-go project. We used the
"prototype first" approach (skip CI/CD, add deploy later):

```bash
agents-cli scaffold create autoagents-agent \
  --agent agentic_rag \
  --datastore agent_platform_search \
  --deployment-target agent_runtime \
  --region us-central1 \
  --agent-guidance-filename CLAUDE.md \
  --prototype --auto-approve

cd autoagents-agent
agents-cli install          # installs Python dependencies
```

This creates an `app/` folder with `agent.py` (the agent), plus tests and config.

> **Note:** The scaffold sets up a **Vertex AI Search** datastore. We later switched
> to **RAG Engine** (see Phase 4), so the Search datastore is leftover — harmless and
> cheap, you can delete it later.

### Phase 3 — Build the agent's brain

We edited `app/agent.py` to:
- Use the model `gemini-3.5-flash`.
- Add **tools** (small Python functions the agent can call): `send_email`,
  `schedule_task`, `list_tasks`, `cancel_task`, `query_messages`,
  `get_agent_state`, `set_agent_state`, `current_time`, `search_documents`,
  `ingest_document`.
- Add **Memory Bank**: a `PreloadMemoryTool()` (loads relevant memories each turn)
  plus an `after_agent_callback` that saves the conversation to memory.

The tools live in `app/tools.py`. Config (project, bucket, sender email) lives in
`app/config.py`, with local values in a `.env` file.

Quick test without deploying:
```bash
agents-cli run "What is the current UTC time? Use your current_time tool."
```

### Phase 4 — RAG Engine (document search / vector store)

RAG Engine is where uploaded documents are stored and searched. New projects can't
use the default ("Spanner") mode in `us-central1`, so we used **Serverless** mode and
put the corpus in `us-west1`:

```bash
# scripts/setup_rag_corpus.py does this for you (idempotent):
uv run python scripts/setup_rag_corpus.py
# It prints: CORPUS projects/<num>/locations/us-west1/ragCorpora/<id>
```

Put that corpus name in `.env` as `RAG_CORPUS=...` and `RAG_LOCATION=us-west1`.

The agent can now `ingest_document("gs://...")` and `search_documents("query")`.

### Phase 5 — Firestore indexes

Two queries need composite indexes (Firestore tells you if one is missing):
```bash
gcloud firestore indexes composite create --collection-group=tasks \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=due_at,order=ascending

gcloud firestore indexes composite create --collection-group=messages \
  --field-config=field-path=channel,order=ascending \
  --field-config=field-path=ts,order=descending
```

### Phase 6 — Email (Resend)

```bash
resend login                       # opens browser, creates + stores an API key
resend domains list                # confirm jmkn.tech is "verified" for sending
```

Store the API key in Secret Manager so the cloud services can use it safely:
```bash
# (the key is in your local .env after login)
printf '%s' "$RESEND_API_KEY" | gcloud secrets create resend-api-key --data-file=-
```

### Phase 7 — Deploy the brain (Agent Runtime)

```bash
# Give the Agent Runtime service account permission to use Firestore/RAG/GCS/secrets
SA="serviceAccount:service-323512451403@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
for role in datastore.user aiplatform.user storage.objectAdmin \
            secretmanager.secretAccessor logging.logWriter; do
  gcloud projects add-iam-policy-binding autoagents-500500 \
    --member="$SA" --role="roles/$role" --condition=None
done

# Deploy (takes 5-10 min; --no-wait + --status avoids command timeouts)
agents-cli deploy --project autoagents-500500 --region us-central1 \
  --no-confirm-project --no-wait \
  --secrets "RESEND_API_KEY=resend-api-key" \
  --update-env-vars "RAG_CORPUS=<corpus>,RAG_LOCATION=us-west1,ATTACHMENTS_BUCKET=autoagents-500500-attachments,SENDER_EMAIL=assistant@jmkn.tech"

agents-cli deploy --status   # poll until it says successful
```

This prints the **engine resource name**, e.g.
`projects/323512451403/locations/us-central1/reasoningEngines/5931933951328256000`.
You'll need it for the gateway.

### Phase 8 — Deploy the gateway (Cloud Run)

The gateway code is in the `gateway/` folder (FastAPI). Create a service account,
a secret random token for the scheduler, then deploy:

```bash
# Service account for the gateway
gcloud iam service-accounts create autoagents-gateway
GSA="serviceAccount:autoagents-gateway@autoagents-500500.iam.gserviceaccount.com"
for role in aiplatform.user datastore.user storage.objectAdmin \
            secretmanager.secretAccessor logging.logWriter; do
  gcloud projects add-iam-policy-binding autoagents-500500 \
    --member="$GSA" --role="roles/$role" --condition=None
done

# Random shared secret so only Cloud Scheduler can trigger /tasks/run
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create tasks-token --data-file=-

# Deploy. --allow-unauthenticated is REQUIRED because Resend posts to the webhook
# with no Google login (the request is verified by signature instead).
gcloud run deploy autoagents-gateway --source gateway/ --region us-central1 \
  --service-account autoagents-gateway@autoagents-500500.iam.gserviceaccount.com \
  --allow-unauthenticated --memory 1Gi \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=autoagents-500500,GOOGLE_CLOUD_LOCATION=us-central1,AGENT_ENGINE_RESOURCE=<engine>,ATTACHMENTS_BUCKET=autoagents-500500-attachments,SENDER_EMAIL=assistant@jmkn.tech,ADMIN_EMAILS=you@gmail.com" \
  --set-secrets "RESEND_API_KEY=resend-api-key:latest,TASKS_TOKEN=tasks-token:latest"
```

It prints the gateway URL, e.g.
`https://autoagents-gateway-323512451403.us-central1.run.app`.

### Phase 9 — Connect Resend inbound + scheduler

```bash
# Create the webhook so Resend forwards received emails to the gateway
resend webhooks create \
  --endpoint https://<gateway-url>/inbound/email --events email.received
# Save the printed signing_secret into Secret Manager:
printf '%s' "<signing_secret>" | gcloud secrets create resend-webhook-secret --data-file=-
# Redeploy the gateway adding: --set-secrets "...,RESEND_WEBHOOK_SECRET=resend-webhook-secret:latest"

# Enable receiving on the domain (CLI can't; use REST or the Resend dashboard)
curl -s -X PATCH "https://api.resend.com/domains/<domain-id>" \
  -H "Authorization: Bearer $RESEND_API_KEY" -H "Content-Type: application/json" \
  -d '{"receiving":true}'

# Add the inbound MX record at your DNS provider (Cloudflare):
#   Type: MX | Name: @ | Target: inbound-smtp.us-east-1.amazonaws.com | Priority: 10

# Scheduler: run due tasks every 5 minutes
TOKEN=$(gcloud secrets versions access latest --secret=tasks-token)
gcloud scheduler jobs create http autoagents-tasks-tick --location=us-central1 \
  --schedule="*/5 * * * *" --http-method=POST \
  --uri="https://<gateway-url>/tasks/run" \
  --headers="X-Tasks-Token=${TOKEN}" --attempt-deadline=180s
```

### Phase 10 — Test it

Email `assistant@jmkn.tech` from your Gmail with "remember my favorite color is teal".
Within a few seconds you get a reply. Send a second email "what's my favorite color?"
to confirm Memory Bank. Attach an image and ask "describe this" to confirm multimodal.

### Phase 11 — WhatsApp channel (optional)

WhatsApp uses **Baileys** (unofficial WhatsApp Web). It needs an always-on process,
so it runs as a container on a **free e2-micro VM**. **Use a dedicated/secondary
number** — unofficial access is against WhatsApp's ToS (ban risk), so keep your
main account out of it.

1. **Build + push the bridge image** (code is in `whatsapp-bridge/`):
```bash
gcloud artifacts repositories create autoagents --repository-format=docker --location=us-central1
gcloud builds submit --tag us-central1-docker.pkg.dev/autoagents-500500/autoagents/whatsapp-bridge:latest whatsapp-bridge/
```

2. **Shared secret + static IP + open the port:**
```bash
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create whatsapp-bridge-secret --data-file=-
gcloud compute addresses create autoagents-wa-ip --region=us-central1   # prints the IP
gcloud compute firewall-rules create allow-wa-bridge --direction=INGRESS --action=ALLOW \
  --rules=tcp:8080 --target-tags=wa-bridge --source-ranges=0.0.0.0/0
```

3. **Let the VM's service account pull the image** (it already has GCS access):
```bash
gcloud projects add-iam-policy-binding autoagents-500500 \
  --member="serviceAccount:autoagents-gateway@autoagents-500500.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.reader"
```

4. **Create the e2-micro VM running the container:**
```bash
WA=$(gcloud secrets versions access latest --secret=whatsapp-bridge-secret)
gcloud compute instances create-with-container autoagents-wa --zone=us-central1-a \
  --machine-type=e2-micro \
  --container-image=us-central1-docker.pkg.dev/autoagents-500500/autoagents/whatsapp-bridge:latest \
  --container-env=GCS_BUCKET=autoagents-500500-attachments,WA_AUTH_PREFIX=wa-auth/,GATEWAY_INBOUND_URL=https://<gateway-url>/inbound/whatsapp,WA_SECRET=$WA,AUTH_DIR=/data/auth,PORT=8080 \
  --service-account=autoagents-gateway@autoagents-500500.iam.gserviceaccount.com --scopes=cloud-platform \
  --address=<static-ip> --tags=wa-bridge --boot-disk-size=10GB
```

5. **Tell the gateway + agent about the bridge** — redeploy each, adding env
   `WHATSAPP_BRIDGE_URL=http://<static-ip>:8080` and secret
   `WHATSAPP_BRIDGE_SECRET=whatsapp-bridge-secret:latest`.

6. **Pair (one time):** open `http://<static-ip>:8080/qr?token=<secret>` in a browser —
   it shows a **live, auto-refreshing QR**. On the dedicated number's phone:
   WhatsApp → Settings → **Linked Devices → Link a Device** → scan the **on-screen** QR
   (not a screenshot — it rotates every ~20s). After linking, the session is saved to
   Cloud Storage, so restarts reconnect automatically (no re-scan).

7. **Test:** message the dedicated number from another phone → you get a reply.

---

## 5. Parameters reference (exact values we used)

| Thing | Value |
|-------|-------|
| GCP project ID | `autoagents-500500` (number `323512451403`) |
| Region | `us-central1` |
| Model | `gemini-3.5-flash` (model location: `global`) |
| RAG corpus | `projects/323512451403/locations/us-west1/ragCorpora/4611686018427387904` (Serverless/Basic tier) |
| Agent Runtime engine | `projects/323512451403/locations/us-central1/reasoningEngines/5931933951328256000` |
| Gateway URL | `https://autoagents-gateway-323512451403.us-central1.run.app` |
| Firestore | Native mode, `us-central1`; collections: `messages`, `tasks`, `agent_state`, `contacts` |
| GCS buckets | `autoagents-500500-attachments`, `autoagents-500500-autoagents-agent-docs` |
| Secrets | `resend-api-key`, `resend-webhook-secret`, `tasks-token` |
| Resend domain | `jmkn.tech` (sending + receiving enabled) |
| Inbound MX | `inbound-smtp.us-east-1.amazonaws.com`, priority 10, on `@` |
| Resend webhook | event `email.received` → `/inbound/email` |
| Scheduler | `autoagents-tasks-tick`, `*/5 * * * *`, POST `/tasks/run` |
| Agent Runtime sizing | cpu 1, memory 4Gi, min 1, max 10, concurrency 8, workers 1 |
| Gateway sizing | memory 1Gi (Cloud Run defaults otherwise) |
| Sender address | `assistant@jmkn.tech` |
| WhatsApp method | Baileys (unofficial WhatsApp Web), dedicated number `+44 7340 926493` |
| WhatsApp VM | `autoagents-wa`, e2-micro, us-central1-a, static IP `136.114.229.113`, port 8080 |
| WhatsApp image | `us-central1-docker.pkg.dev/autoagents-500500/autoagents/whatsapp-bridge:latest` |
| WhatsApp secret | `whatsapp-bridge-secret` (guards `/send` + `/qr`) |
| WhatsApp auth | persisted to `gs://autoagents-500500-attachments/wa-auth/` |
| Gateway WA endpoint | `POST /inbound/whatsapp` (bridge → gateway, `X-WA-Secret`) |

---

## 6. How to operate it

- **Talk to it:** email `assistant@jmkn.tech`, or WhatsApp `+44 7340 926493`. Attach
  images/PDF/audio/video freely on either channel.
- **Admin commands** (from an allow-listed address — set by `ADMIN_EMAILS`):
  - `!status` — current state + how many tasks are due
  - `!pause` — keep logging but stop acting
  - `!resume` — resume
  - `!stop` — ignore non-admin input until resumed
- **Give high-level instructions:** just email them, e.g. "Follow up with John about
  the invoice by Friday and summarize the replies." It creates tasks and works over time.

### WhatsApp operations
- **Re-pair** (only if the session is lost): open `http://136.114.229.113:8080/qr?token=<secret>`
  and scan again. Normally not needed — auth survives restarts via Cloud Storage.
- **Restart the bridge:** `gcloud compute instances reset autoagents-wa --zone=us-central1-a`
  (it pulls `:latest` and reconnects from saved creds).
- **Bridge health:** `curl http://136.114.229.113:8080/health` → `{"status":"ok","connected":true}`.
- **Bridge logs:** Cloud Logging, filter `logName=~"cos_containers"` (the container's stdout).
- **Update the bridge code:** rebuild + push the image, then `reset` the VM.

### See what it's doing
- **Messages/tasks:** Firestore console → collections `messages`, `tasks`, `agent_state`.
  WhatsApp messages are logged with `channel="whatsapp"`.
- **Gateway logs:** Cloud Run → `autoagents-gateway` → Logs.
- **Brain logs:** Cloud Logging, filter `resource.type=aiplatform.googleapis.com/ReasoningEngine`.

---

## 7. Troubleshooting (real issues we hit)

| Symptom | Cause & fix |
|---------|-------------|
| `brew install terraform` does nothing | Formula removed; download the binary (see §2). |
| RAG `create_corpus` → "Spanner mode restricted" | New projects can't use Spanner RAG in us-central1. Use Serverless tier (`Basic`) and/or region `us-west1`. |
| Datastore provisioning fails with 503 | Transient. Just re-run `agents-cli infra datastore` — Terraform resumes. |
| `/healthz` returns a Google 404 | Google's edge reserves `/healthz`. Name your health route `/health`. |
| Inbound email body empty | Resend's `email.received` webhook is **metadata-only**; fetch the full email via `GET /emails/receiving/{id}`. |
| Attachments not picked up | Get them from `GET /emails/receiving/{id}/attachments` — each has a `download_url`. |
| Env var with a comma breaks deploy | Cloud Run/Agent Runtime split env vars on commas. We used `;` for `ADMIN_EMAILS` and split on both in code. |
| Cloud Run deploy blocked for `--allow-unauthenticated` | This is intentional (it makes a public endpoint). Required for the Resend webhook; protected by signature + token. |
| Agent works but a tool errors after deploy | The runtime service account is missing an IAM role (Firestore/RAG/GCS/secret). Grant it (see Phase 7). |
| Agent says "database (default) does not exist" but the action (e.g. email) actually happened | Agent Runtime gives the project **number**; Firestore's data API needs the project **ID**. Coerce a numeric project to the ID in `config.py`. Also make logging best-effort so a logging failure doesn't report the whole action as failed. |
| WhatsApp container won't start ("downloadArtifacts denied") | The VM's service account lacks Artifact Registry read. Grant `roles/artifactregistry.reader`, then `reset` the VM. |
| WhatsApp "couldn't link device, try again later" | The QR rotates (~20s). Scan the **live** `/qr` page's on-screen QR, not a screenshot. If it persists, you've hit WhatsApp's throttle from repeated attempts — wait a few minutes. |
| Bridge keeps flapping / `/health` intermittent | The e2-micro is being flooded by auth backups. Backups must be **debounced + sequential + non-resumable** (already fixed in `whatsapp-bridge/index.js`). |
| WhatsApp disconnects after pairing (close code 515) | Normal — that's WhatsApp's "restart required" after linking. The bridge reconnects from saved creds automatically. |

---

## 8. File map (in this repo)

```
autoagents/
├── .agents-cli-spec.md      # the agent's spec (what it is)
├── steps.md                 # running build log
├── docs/
│   ├── HUMAN_GUIDE.md       # this file
│   └── AGENT_GUIDE.md       # machine-oriented version
├── autoagents-agent/        # the brain (ADK project)
│   ├── app/
│   │   ├── agent.py         # the agent: model, instruction, tools, Memory Bank
│   │   ├── tools.py         # send_email, schedule_task, search_documents, ...
│   │   ├── config.py        # central config
│   │   ├── retrievers.py    # (scaffold) Vertex AI Search tool — unused now
│   │   └── mcp_server.py    # exposes the tools over MCP
│   ├── scripts/setup_rag_corpus.py
│   ├── firestore.indexes.json
│   └── .env                 # local config + secrets (gitignored)
├── gateway/                 # the Cloud Run service
│   ├── main.py              # /inbound/email, /inbound/whatsapp, /tasks/run, /health
│   ├── clients.py           # Firestore, GCS, Resend, WhatsApp, Agent Runtime helpers
│   ├── config.py
│   ├── Dockerfile
│   └── requirements.txt
└── whatsapp-bridge/         # the Baileys bridge (Node) on the e2-micro VM
    ├── index.js             # WhatsApp connection, /qr, /send, /health, GCS auth
    ├── package.json
    └── Dockerfile
```

---

## 9. Live channels & what's deferred

**Live:** Email (Resend) + WhatsApp (Baileys bridge). Both share the same brain,
memory, RAG, scheduling, and logging.

**Deferred (v2):**
- **Voice calls** — need a provider/budget decision (e.g. Twilio). Not free.
- **WhatsApp groups** — DMs only for now; group policy + WhatsApp admin commands later.
- **Loop guard** — ignore self-sent email (defensive).
- **Daily digest** — a scheduled summary email.
- **Observability dashboards** — Cloud Trace / BigQuery analytics.
- **Rotate `whatsapp-bridge-secret`** — it was shown in chat during pairing.
```
