# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Central configuration for the autoagents assistant.

Non-secret config comes from environment variables (with sane defaults).
Secrets (RESEND_API_KEY) are injected at runtime from Secret Manager in
deployment, or from a local .env during development.
"""

import os

# --- Model ---
LLM = "gemini-3.5-flash"
LLM_LOCATION = "global"
REGION = "us-central1"

# --- GCP ---
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "autoagents-500500")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
ATTACHMENTS_BUCKET = os.environ.get(
    "ATTACHMENTS_BUCKET", "autoagents-500500-attachments"
)

# --- RAG Engine (long-term document vector store) ---
# Corpus lives in us-west1 (us-central1 RAG Engine is capacity-restricted for
# new projects). Set RAG_CORPUS to the full ragCorpora/... resource name.
RAG_LOCATION = os.environ.get("RAG_LOCATION", "us-west1")
RAG_CORPUS = os.environ.get("RAG_CORPUS", "")

# --- Email (Resend) ---
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "assistant@jmkn.tech")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# --- Admin allowlist (who may issue !pause/!stop and send-on-behalf) ---
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get(
        "ADMIN_EMAILS", "shahirshamim15314@gmail.com,jmkntech@gmail.com"
    ).split(",")
    if e.strip()
]

# --- Firestore collections ---
COL_MESSAGES = "messages"
COL_TASKS = "tasks"
COL_STATE = "agent_state"
COL_CONTACTS = "contacts"
STATE_DOC_ID = "singleton"
