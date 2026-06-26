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

**Multi-tenant:** every tool reads the active ``tenant_id`` (and the tenant's RAG
corpus) from ``ToolContext.state``, which the gateway injects into the session.
All Firestore reads/writes and RAG retrieval are scoped to that tenant so one
tenant can never see another's documents, tasks, messages, or run-state. When no
context is present (e.g. a direct/local call) the helpers fall back to the
default tenant, never to an unscoped query.

All side-effecting actions (emails sent, tasks created) are logged to Firestore
so the audit trail in the ``messages``/``tasks`` collections is complete.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from typing import Any

import requests
from google.adk.tools.tool_context import ToolContext
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
# Tenancy helpers — resolve the caller's tenant from injected session state
# --------------------------------------------------------------------------- #
def _tenant(tool_context: ToolContext | None) -> str:
    """Active tenant id from session state; falls back to the owner tenant.

    The gateway sets ``state = {"tenant_id": ..., "rag_corpus": ...}`` when it
    creates the Agent Runtime session, so every tool call is tenant-scoped.
    """
    if tool_context is not None:
        tid = (getattr(tool_context, "state", None) or {}).get("tenant_id")
        if tid:
            return str(tid)
    return config.DEFAULT_TENANT


def _corpus(tool_context: ToolContext | None) -> str:
    """The active tenant's RAG corpus (injected into state), else the default."""
    if tool_context is not None:
        c = (getattr(tool_context, "state", None) or {}).get("rag_corpus")
        if c:
            return str(c)
    return config.RAG_CORPUS


def _tagged_sender(tenant_id: str) -> str:
    """Reply-routable from-address ``assistant+<tenant_id>@jmkn.tech``.

    A reply to this address carries the tenant tag so the gateway can route a
    third party's response back to the initiating tenant (Phase 4).
    """
    user, _, domain = config.SENDER_EMAIL.partition("@")
    return f"{user}+{tenant_id}@{domain}" if domain else config.SENDER_EMAIL


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
    tenant_id: str = "",
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
        tenant_id: Owning tenant (for per-tenant audit isolation).

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
            "tenant_id": tenant_id,
            "ts": _now(),
        }
    )
    return doc_id


def send_email(
    to: str, subject: str, body: str, tool_context: ToolContext = None
) -> dict[str, Any]:
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
    tenant_id = _tenant(tool_context)
    sender = _tagged_sender(tenant_id)
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=30,
        )
        ok = resp.status_code in (200, 201)
        data = resp.json() if resp.content else {}
        msg_id = data.get("id", "")
        try:
            log_message(
                channel="email",
                direction="out",
                sender=sender,
                recipient=to,
                subject=subject,
                body=body,
                status="sent" if ok else f"error:{resp.status_code}",
                tenant_id=tenant_id,
            )
        except Exception:
            pass
        if ok:
            return {"ok": True, "id": msg_id}
        return {"ok": False, "error": f"{resp.status_code}: {resp.text[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_whatsapp(
    to: str, text: str, tool_context: ToolContext = None
) -> dict[str, Any]:
    """Send a WhatsApp message on the user's behalf via the bridge.

    Use this when the user asks you to message someone on WhatsApp. Provide the
    recipient's phone number in international format (digits, optionally with +),
    e.g. "15551234567".

    Args:
        to: Recipient phone number (international format).
        text: Message text.

    Returns:
        A dict with "ok" and either "id" or "error".
    """
    if not (config.WHATSAPP_BRIDGE_URL and config.WHATSAPP_BRIDGE_SECRET):
        return {"ok": False, "error": "WhatsApp bridge not configured"}
    tenant_id = _tenant(tool_context)
    ok = False
    data: dict[str, Any] = {}
    try:
        resp = requests.post(
            config.WHATSAPP_BRIDGE_URL.rstrip("/") + "/send",
            headers={"X-WA-Secret": config.WHATSAPP_BRIDGE_SECRET},
            json={"to": to, "text": text},
            timeout=30,
        )
        ok = resp.status_code in (200, 201)
        data = resp.json() if resp.content else {}
    except Exception as exc:  # noqa: BLE001
        data = {"error": str(exc)}
    try:
        log_message(
            channel="whatsapp",
            direction="out",
            sender="agent",
            recipient=to,
            body=text,
            status="sent" if ok else "error",
            tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001 - logging must never mask a real send
        pass
    if ok:
        return {"ok": True, "id": data.get("id", "")}
    return {"ok": False, "error": data.get("error", "send failed")}


def query_messages(
    channel: str = "", limit: int = 20, tool_context: ToolContext = None
) -> dict[str, Any]:
    """Look up recent logged messages for this user, optionally by channel.

    Args:
        channel: Filter by "email"/"whatsapp"/"call"; empty for all channels.
        limit: Maximum number of messages to return (most recent first).

    Returns:
        A dict with "messages": a list of message records.
    """
    tenant_id = _tenant(tool_context)
    # Filter by tenant on a single-field index, then sort/slice in-process so we
    # don't depend on a per-(tenant,channel,ts) composite index.
    docs = [
        d.to_dict() | {"id": d.id}
        for d in _client()
        .collection(config.COL_MESSAGES)
        .where("tenant_id", "==", tenant_id)
        .stream()
    ]
    if channel:
        docs = [m for m in docs if m.get("channel") == channel]
    docs.sort(key=lambda m: m.get("ts", ""), reverse=True)
    return {"messages": docs[: int(limit)]}


# --------------------------------------------------------------------------- #
# Tasks / reminders / followups
# --------------------------------------------------------------------------- #
def schedule_task(
    description: str,
    due_at: str,
    task_type: str = "task",
    recurrence: str = "",
    tool_context: ToolContext = None,
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
    tenant_id = _tenant(tool_context)
    doc_id = uuid.uuid4().hex
    _client().collection(config.COL_TASKS).document(doc_id).set(
        {
            "type": task_type,
            "description": description,
            "due_at": due_at,
            "recurrence": recurrence,
            "status": "pending",
            "tenant_id": tenant_id,
            "created_at": _now(),
        }
    )
    return {"ok": True, "id": doc_id}


def list_tasks(
    status: str = "pending", tool_context: ToolContext = None
) -> dict[str, Any]:
    """List this user's scheduled tasks, optionally filtered by status.

    Args:
        status: "pending", "done", "cancelled", or "all".

    Returns:
        A dict with "tasks": a list of task records, soonest due first.
    """
    tenant_id = _tenant(tool_context)
    docs = [
        d.to_dict() | {"id": d.id}
        for d in _client()
        .collection(config.COL_TASKS)
        .where("tenant_id", "==", tenant_id)
        .stream()
    ]
    if status and status != "all":
        docs = [t for t in docs if t.get("status") == status]
    docs.sort(key=lambda t: t.get("due_at", ""))
    return {"tasks": docs[:100]}


def cancel_task(task_id: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Cancel one of this user's scheduled tasks by its id.

    Args:
        task_id: The id returned by schedule_task / list_tasks.

    Returns:
        A dict with "ok".
    """
    tenant_id = _tenant(tool_context)
    ref = _client().collection(config.COL_TASKS).document(task_id)
    snap = ref.get()
    # Treat another tenant's task as not found — never act across tenants.
    if not snap.exists or (snap.to_dict() or {}).get("tenant_id") != tenant_id:
        return {"ok": False, "error": "task not found"}
    ref.update({"status": "cancelled", "cancelled_at": _now()})
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Agent lifecycle state (start / pause / stop) — per tenant
# --------------------------------------------------------------------------- #
def get_agent_state(tool_context: ToolContext = None) -> dict[str, Any]:
    """Return this user's agent run state ("running", "paused", "stopped")."""
    tenant_id = _tenant(tool_context)
    snap = _client().collection(config.COL_STATE).document(tenant_id).get()
    if not snap.exists:
        return {"status": "running", "reason": "default"}
    return snap.to_dict()


def set_agent_state(
    status: str, reason: str = "", tool_context: ToolContext = None
) -> dict[str, Any]:
    """Set this user's agent run state. Admin-only; the gateway enforces who may.

    Args:
        status: "running", "paused", or "stopped".
        reason: Optional human reason for the change.

    Returns:
        A dict with "ok" and the new "status".
    """
    if status not in ("running", "paused", "stopped"):
        return {"ok": False, "error": f"invalid status {status!r}"}
    tenant_id = _tenant(tool_context)
    _client().collection(config.COL_STATE).document(tenant_id).set(
        {"status": status, "reason": reason, "updated_at": _now()}
    )
    return {"ok": True, "status": status}


def current_time() -> dict[str, Any]:
    """Return the current UTC date and time as an ISO-8601 string."""
    return {"now": _now()}


# --------------------------------------------------------------------------- #
# Web search (Google-Search-grounded Gemini) — gated on user consent
# --------------------------------------------------------------------------- #
def web_search(query: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Search the public web and return a grounded answer with sources.

    CONSENT REQUIRED: only call this AFTER the user has explicitly agreed to a
    web search for the current request. If the user has not yet approved
    searching the web, do NOT call this — first ask them for permission and wait
    for their explicit yes.

    Args:
        query: The web search query / question to look up.

    Returns:
        A dict with "ok", "answer" (a grounded summary), and "sources" (a list
        of {title, uri}); or "ok": False and "error".
    """
    try:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(
            vertexai=True, project=config.PROJECT_ID, location=config.LLM_LOCATION
        )
        resp = client.models.generate_content(
            model=config.WEB_SEARCH_MODEL,
            contents=query,
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
            ),
        )
        answer = getattr(resp, "text", "") or ""
        sources: list[dict[str, str]] = []
        cand = (getattr(resp, "candidates", None) or [None])[0]
        gm = getattr(cand, "grounding_metadata", None)
        for chunk in getattr(gm, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web:
                sources.append(
                    {
                        "title": getattr(web, "title", "") or "",
                        "uri": getattr(web, "uri", "") or "",
                    }
                )
        return {"ok": True, "answer": answer, "sources": sources}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Long-term document store (Vertex AI RAG Engine) — per tenant corpus
# --------------------------------------------------------------------------- #
_rag_inited = False


def _ensure_rag() -> None:
    global _rag_inited
    if not _rag_inited:
        import vertexai

        vertexai.init(project=config.PROJECT_ID, location=config.RAG_LOCATION)
        _rag_inited = True


def search_documents(
    query: str, top_k: int = 5, tool_context: ToolContext = None
) -> dict[str, Any]:
    """Search this user's long-term document store (RAG Engine) for relevant text.

    Args:
        query: What to look for.
        top_k: How many passages to return.

    Returns:
        A dict with "contexts": a list of {text, source} passages.
    """
    corpus = _corpus(tool_context)
    if not corpus:
        return {"ok": False, "error": "no document store for this user", "contexts": []}
    from vertexai import rag

    _ensure_rag()
    resp = rag.retrieval_query(
        text=query,
        rag_resources=[rag.RagResource(rag_corpus=corpus)],
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


def ingest_document(
    gcs_uri: str, display_name: str = "", tool_context: ToolContext = None
) -> dict[str, Any]:
    """Add a document to this user's long-term store by its Cloud Storage URI.

    Args:
        gcs_uri: A gs:// URI of a file already in Cloud Storage (e.g. an email
            attachment the gateway stored).
        display_name: Optional human label for logs.

    Returns:
        A dict with "ok" and the number of files imported.
    """
    corpus = _corpus(tool_context)
    if not corpus:
        return {"ok": False, "error": "no document store for this user"}
    from vertexai import rag

    _ensure_rag()
    try:
        resp = rag.import_files(corpus_name=corpus, paths=[gcs_uri])
        return {
            "ok": True,
            "imported": getattr(resp, "imported_rag_files_count", 0),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
