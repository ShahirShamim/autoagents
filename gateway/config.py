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

# Resend
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "assistant@jmkn.tech")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
# Svix signing secret for Resend inbound webhooks (whsec_...).
RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "")

# Agent Runtime resource name, e.g.
# projects/<num>/locations/us-central1/reasoningEngines/<id>
# Set after `agents-cli deploy`.
AGENT_ENGINE_RESOURCE = os.environ.get("AGENT_ENGINE_RESOURCE", "")

# Who may issue admin commands / be sent agent output proactively.
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get(
        "ADMIN_EMAILS", "shahirshamim15314@gmail.com,jmkntech@gmail.com"
    ).split(",")
    if e.strip()
]

# Firestore collections (kept in sync with app/config.py)
COL_MESSAGES = "messages"
COL_TASKS = "tasks"
COL_STATE = "agent_state"
STATE_DOC_ID = "singleton"
