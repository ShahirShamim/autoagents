#!/usr/bin/env bash
# Deploy the autoagents Agent Runtime engine.
#
# Runtime config (bridge URL + API secrets) all live in Secret Manager and are
# wired in as secretEnv. secretEnv is sticky across deploys — it survives even a
# bare `agents-cli deploy` — so the runtime config can no longer drift or get
# wiped (which used to break WhatsApp with "WhatsApp bridge not configured").
#
# Prefer this wrapper, but a bare deploy is now safe too: secretEnv persists.
#
# If the bridge host/IP changes, update the secret (NOT this file), then redeploy:
#   printf 'http://NEW_IP:8080' | gcloud secrets versions add whatsapp-bridge-url \
#     --project=autoagents-500500 --data-file=-
#   ./deploy.sh
set -euo pipefail

PROJECT="${PROJECT:-autoagents-500500}"

exec agents-cli deploy \
  --project "$PROJECT" \
  --no-confirm-project \
  --secrets RESEND_API_KEY=resend-api-key,WHATSAPP_BRIDGE_SECRET=whatsapp-bridge-secret,WHATSAPP_BRIDGE_URL=whatsapp-bridge-url \
  "$@"
