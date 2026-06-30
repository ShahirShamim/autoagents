"""Gateway configuration (Cloud Run service).

The gateway is the event/webhook layer that Agent Runtime cannot host:
inbound email (Resend webhook) and the scheduler tick. It shares Firestore
collections and config conventions with the agent project.
"""
import os

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "autoagents-500500")
REGION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
ATTACHMENTS_BUCKET = os.environ.get(
    "ATTACHMENTS_BUCKET", "autoagents-500500-attachments"
)

# RAG Engine region (corpora live here; us-central1 is capacity-restricted for
# new projects). Used to auto-provision a per-tenant corpus at onboarding.
RAG_LOCATION = os.environ.get("RAG_LOCATION", "us-west1")

# Resend
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "assistant@jmkn.tech")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
# Svix signing secret for Resend inbound webhooks (whsec_...).
RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "")

# Shared secret Cloud Scheduler sends in the X-Tasks-Token header to authorise
# /tasks/run (the service is public for the Resend webhook, so this gates the
# scheduler endpoint).
TASKS_TOKEN = os.environ.get("TASKS_TOKEN", "")

# --- WhatsApp bridge (Baileys) ---
WHATSAPP_BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "")
WHATSAPP_BRIDGE_SECRET = os.environ.get("WHATSAPP_BRIDGE_SECRET", "")

# Signs the per-tenant self-service WhatsApp-linking magic links (/link?token=).
LINK_SECRET = os.environ.get("LINK_SECRET", "")
# Public base URL of this gateway, used to build the magic link in emails.
# Custom domain (Cloud Run domain mapping → Cloudflare); env-overridable.
GATEWAY_PUBLIC_URL = os.environ.get("GATEWAY_PUBLIC_URL", "https://autoagents.jmkn.tech")
# Magic link validity (days).
LINK_MAX_AGE_DAYS = int(os.environ.get("LINK_MAX_AGE_DAYS", "30"))
ADMIN_WHATSAPP = [
    n.strip()
    for n in os.environ.get("ADMIN_WHATSAPP", "").replace(";", ",").split(",")
    if n.strip()
]

# Agent Runtime resource name, e.g.
# projects/<num>/locations/us-central1/reasoningEngines/<id>
# Set after `agents-cli deploy`.
AGENT_ENGINE_RESOURCE = os.environ.get("AGENT_ENGINE_RESOURCE", "")

# Who may issue admin commands / be sent agent output proactively.
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get(
        "ADMIN_EMAILS", "shahirshamim15314@gmail.com,jmkntech@gmail.com"
    )
    .replace(";", ",")
    .split(",")
    if e.strip()
]

# Firestore collections (kept in sync with app/config.py)
COL_MESSAGES = "messages"
COL_TASKS = "tasks"
COL_STATE = "agent_state"
COL_CONTACTS = "contacts"
COL_USAGE = "usage"  # per-turn token-usage records, keyed by tenant_id
COL_SESSIONS = "agent_sessions"  # per-tenant active-session pointer {session_id, last_at}
COL_ALERTS = "alerts"  # operational issues surfaced in the admin panel
STATE_DOC_ID = "singleton"

# Start a fresh Agent Runtime session once a tenant has been idle this long. On
# rotation the old session is flushed to long-term memory first (and rotation is
# skipped if that flush fails, so nothing is lost).
SESSION_IDLE_HOURS = int(os.environ.get("SESSION_IDLE_HOURS", "8"))

# --- Multi-tenant registry (Phase 1) ---
COL_TENANTS = "tenants"
COL_IDENTITIES = "identities"
COL_THREADS = "threads"
# The original single-tenant user is migrated to this tenant id.
DEFAULT_TENANT = "tenant_0"

# --- Third-party reply threads (Phase 4) ---
# A third party (someone the agent emailed on a tenant's behalf) may converse
# with the agent only for this many hours after their FIRST reply. A fresh
# outbound to the same contact reopens the window.
THREAD_TTL_HOURS = int(os.environ.get("THREAD_TTL_HOURS", "3"))
