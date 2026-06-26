"""Config for the autoagents admin webapp (Cloud Run service `autoagents-admin`).

Shares the same Firestore collections as the gateway + agent. A single shared
password (Secret Manager `admin-password`) gates the whole UI.
"""
import os

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "autoagents-500500")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")

# Shared admin password, injected from Secret Manager at deploy.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

COOKIE_NAME = "aa_admin"
SESSION_MAX_AGE = 60 * 60 * 8  # 8 hours

# Gateway (for the "Send WhatsApp link" action, which the gateway mints + emails).
GATEWAY_URL = os.environ.get(
    "GATEWAY_URL", "https://autoagents-gateway-323512451403.us-central1.run.app"
)
TASKS_TOKEN = os.environ.get("TASKS_TOKEN", "")
# The service is private (accessed via `gcloud run services proxy`), so the
# browser talks to http://localhost — a Secure cookie wouldn't survive that.
# Set COOKIE_SECURE=true only if you later expose the app over public HTTPS.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"

# Firestore collections (kept in sync with gateway/app config).
COL_TENANTS = "tenants"
COL_IDENTITIES = "identities"
COL_STATE = "agent_state"
COL_MESSAGES = "messages"
COL_TASKS = "tasks"
COL_USAGE = "usage"
COL_ALERTS = "alerts"

# Cost-estimate rates (USD per 1M tokens). Left at 0 → the UI shows token counts
# only (no $). Set real gemini-3.5-flash rates to surface an estimated cost.
LLM_INPUT_COST_PER_1M = float(os.environ.get("LLM_INPUT_COST_PER_1M", "0") or 0)
LLM_OUTPUT_COST_PER_1M = float(os.environ.get("LLM_OUTPUT_COST_PER_1M", "0") or 0)
