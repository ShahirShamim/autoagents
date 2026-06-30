"""Config for the autoagents admin webapp (Cloud Run service `autoagents-admin`).

Shares the same Firestore collections as the gateway + agent. Sign-in is an
email magic link restricted to ``ADMIN_EMAIL``; the shared password
(Secret Manager `admin-password`) is kept only as an emergency break-glass.
"""
import os

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "autoagents-500500")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")

# Break-glass password (emergency only), injected from Secret Manager at deploy.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Magic-link sign-in: only this address may request a login link.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "shahirshamim15314@gmail.com").strip().lower()
# Signs session cookies + magic-link tokens, independent of the break-glass
# password. Injected from Secret Manager `admin-magic-secret`.
MAGIC_SECRET = os.environ.get("MAGIC_SECRET", "")
MAGIC_MAX_AGE = 60 * 15  # a magic link is valid for 15 minutes
# Public origin of this admin app, for building magic-link URLs in emails.
ADMIN_PUBLIC_URL = os.environ.get("ADMIN_PUBLIC_URL", "https://admin.autoagents.jmkn.tech")
# Resend (sends the magic-link email). Injected from Secret Manager `resend-api-key`.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "assistant@jmkn.tech")

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
