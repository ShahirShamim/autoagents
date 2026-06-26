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
