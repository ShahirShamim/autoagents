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
"""Function tools for the autoagents assistant.

These are plain typed functions registered as ADK FunctionTools on the agent.
They are also re-exported by ``app/mcp_server.py`` so the exact same logic can
be served over MCP for reuse by the Cloud Run gateway or other agents.

All side-effecting actions (emails sent, tasks created) are logged to Firestore
so the audit trail in the ``messages``/``tasks`` collections is complete.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

import requests
from google.cloud import firestore

from app import config

_db: firestore.Client | None = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(
            project=config.PROJECT_ID, database=config.FIRESTORE_DATABASE
        )
    return _db


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
def log_message(
    channel: str,
    direction: str,
    sender: str,
    recipient: str,
    body: str,
    subject: str = "",
    status: str = "logged",
    session_id: str = "",
) -> str:
    """Record one message (email/whatsapp/call, inbound or outbound) to the log.

    Args:
        channel: One of "email", "whatsapp", "call".
        direction: "in" for received, "out" for sent.
        sender: From address/number.
        recipient: To address/number.
        body: Message text or transcript.
        subject: Email subject if applicable.
        status: Delivery status string.
        session_id: Conversation/session id this belongs to.

    Returns:
        The Firestore document id of the logged message.
    """
    doc_id = uuid.uuid4().hex
    _client().collection(config.COL_MESSAGES).document(doc_id).set(
        {
            "channel": channel,
            "direction": direction,
            "from": sender,
            "to": recipient,
            "subject": subject,
            "body": body,
            "status": status,
            "session_id": session_id,
            "ts": _now(),
        }
    )
    return doc_id


def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
    """Send an email on the user's behalf via Resend and log it.

    Only use this when the user has clearly asked you to email someone, or to
    reply/report back to the user. Confirm the recipient, subject, and content
    are what the user intended before sending.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text body of the email.

    Returns:
        A dict with "ok" (bool) and either "id" (Resend message id) or "error".
    """
    if not config.RESEND_API_KEY:
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": config.SENDER_EMAIL,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=30,
        )
        ok = resp.status_code in (200, 201)
        data = resp.json() if resp.content else {}
        msg_id = data.get("id", "")
        log_message(
            channel="email",
            direction="out",
            sender=config.SENDER_EMAIL,
            recipient=to,
            subject=subject,
            body=body,
            status="sent" if ok else f"error:{resp.status_code}",
        )
        if ok:
            return {"ok": True, "id": msg_id}
        return {"ok": False, "error": f"{resp.status_code}: {resp.text[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def query_messages(channel: str = "", limit: int = 20) -> dict[str, Any]:
    """Look up recent logged messages, optionally filtered by channel.

    Args:
        channel: Filter by "email"/"whatsapp"/"call"; empty for all channels.
        limit: Maximum number of messages to return (most recent first).

    Returns:
        A dict with "messages": a list of message records.
    """
    q = _client().collection(config.COL_MESSAGES)
    if channel:
        q = q.where("channel", "==", channel)
    q = q.order_by("ts", direction=firestore.Query.DESCENDING).limit(int(limit))
    return {"messages": [d.to_dict() | {"id": d.id} for d in q.stream()]}


# --------------------------------------------------------------------------- #
# Tasks / reminders / followups
# --------------------------------------------------------------------------- #
def schedule_task(
    description: str,
    due_at: str,
    task_type: str = "task",
    recurrence: str = "",
) -> dict[str, Any]:
    """Schedule a task, reminder, or follow-up to be acted on at a later time.

    Args:
        description: What to do when the task is due (a clear instruction to
            yourself, e.g. "Email the user a reminder to renew jmkn.tech").
        due_at: Absolute ISO-8601 timestamp for when this is due, e.g.
            "2026-07-20T09:00:00+00:00".
        task_type: "task", "reminder", or "followup".
        recurrence: Optional cron-like or human recurrence ("daily", "weekly");
            empty for one-off.

    Returns:
        A dict with "ok" and the created task "id".
    """
    try:
        _dt.datetime.fromisoformat(due_at)
    except ValueError:
        return {"ok": False, "error": f"due_at not valid ISO-8601: {due_at!r}"}
    doc_id = uuid.uuid4().hex
    _client().collection(config.COL_TASKS).document(doc_id).set(
        {
            "type": task_type,
            "description": description,
            "due_at": due_at,
            "recurrence": recurrence,
            "status": "pending",
            "created_at": _now(),
        }
    )
    return {"ok": True, "id": doc_id}


def list_tasks(status: str = "pending") -> dict[str, Any]:
    """List scheduled tasks, optionally filtered by status.

    Args:
        status: "pending", "done", "cancelled", or "all".

    Returns:
        A dict with "tasks": a list of task records, soonest due first.
    """
    q = _client().collection(config.COL_TASKS)
    if status and status != "all":
        q = q.where("status", "==", status)
    q = q.order_by("due_at").limit(100)
    return {"tasks": [d.to_dict() | {"id": d.id} for d in q.stream()]}


def cancel_task(task_id: str) -> dict[str, Any]:
    """Cancel a scheduled task by its id.

    Args:
        task_id: The id returned by schedule_task / list_tasks.

    Returns:
        A dict with "ok".
    """
    ref = _client().collection(config.COL_TASKS).document(task_id)
    if not ref.get().exists:
        return {"ok": False, "error": "task not found"}
    ref.update({"status": "cancelled", "cancelled_at": _now()})
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Agent lifecycle state (start / pause / stop)
# --------------------------------------------------------------------------- #
def get_agent_state() -> dict[str, Any]:
    """Return the agent's current run state ("running", "paused", "stopped")."""
    ref = _client().collection(config.COL_STATE).document(config.STATE_DOC_ID)
    snap = ref.get()
    if not snap.exists:
        return {"status": "running", "reason": "default"}
    return snap.to_dict()


def set_agent_state(status: str, reason: str = "") -> dict[str, Any]:
    """Set the agent's run state. Admin-only; the gateway enforces the allowlist.

    Args:
        status: "running", "paused", or "stopped".
        reason: Optional human reason for the change.

    Returns:
        A dict with "ok" and the new "status".
    """
    if status not in ("running", "paused", "stopped"):
        return {"ok": False, "error": f"invalid status {status!r}"}
    _client().collection(config.COL_STATE).document(config.STATE_DOC_ID).set(
        {"status": status, "reason": reason, "updated_at": _now()}
    )
    return {"ok": True, "status": status}


def current_time() -> dict[str, Any]:
    """Return the current UTC date and time as an ISO-8601 string."""
    return {"now": _now()}


# --------------------------------------------------------------------------- #
# Long-term document store (Vertex AI RAG Engine)
# --------------------------------------------------------------------------- #
_rag_inited = False


def _ensure_rag() -> None:
    global _rag_inited
    if not _rag_inited:
        import vertexai

        vertexai.init(project=config.PROJECT_ID, location=config.RAG_LOCATION)
        _rag_inited = True


def search_documents(query: str, top_k: int = 5) -> dict[str, Any]:
    """Search the user's long-term document store (RAG Engine) for relevant text.

    Args:
        query: What to look for.
        top_k: How many passages to return.

    Returns:
        A dict with "contexts": a list of {text, source} passages.
    """
    if not config.RAG_CORPUS:
        return {"ok": False, "error": "RAG_CORPUS not configured", "contexts": []}
    from vertexai import rag

    _ensure_rag()
    resp = rag.retrieval_query(
        text=query,
        rag_resources=[rag.RagResource(rag_corpus=config.RAG_CORPUS)],
        rag_retrieval_config=rag.RagRetrievalConfig(top_k=int(top_k)),
    )
    contexts = []
    for ctx in getattr(getattr(resp, "contexts", None), "contexts", []) or []:
        contexts.append(
            {
                "text": getattr(ctx, "text", ""),
                "source": getattr(ctx, "source_uri", "")
                or getattr(ctx, "source_display_name", ""),
            }
        )
    return {"ok": True, "contexts": contexts}


def ingest_document(gcs_uri: str, display_name: str = "") -> dict[str, Any]:
    """Add a document to the long-term store by its Cloud Storage URI.

    Args:
        gcs_uri: A gs:// URI of a file already in Cloud Storage (e.g. an email
            attachment the gateway stored).
        display_name: Optional human label for logs.

    Returns:
        A dict with "ok" and the number of files imported.
    """
    if not config.RAG_CORPUS:
        return {"ok": False, "error": "RAG_CORPUS not configured"}
    from vertexai import rag

    _ensure_rag()
    try:
        resp = rag.import_files(corpus_name=config.RAG_CORPUS, paths=[gcs_uri])
        return {
            "ok": True,
            "imported": getattr(resp, "imported_rag_files_count", 0),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
