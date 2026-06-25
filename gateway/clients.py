"""External clients + helpers for the gateway: Firestore, GCS, Resend, Agent Runtime."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import requests
from google.cloud import firestore, storage

from gateway import config

_db: firestore.Client | None = None
_gcs: storage.Client | None = None


def db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(
            project=config.PROJECT_ID, database=config.FIRESTORE_DATABASE
        )
    return _db


def gcs() -> storage.Client:
    global _gcs
    if _gcs is None:
        _gcs = storage.Client(project=config.PROJECT_ID)
    return _gcs


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


# --------------------------------------------------------------------------- #
# Firestore
# --------------------------------------------------------------------------- #
def log_message(
    *,
    channel: str,
    direction: str,
    sender: str,
    recipient: str,
    body: str,
    subject: str = "",
    status: str = "logged",
    session_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    doc_id = uuid.uuid4().hex
    db().collection(config.COL_MESSAGES).document(doc_id).set(
        {
            "channel": channel,
            "direction": direction,
            "from": sender,
            "to": recipient,
            "subject": subject,
            "body": body,
            "status": status,
            "session_id": session_id,
            "attachments": attachments or [],
            "ts": now_iso(),
        }
    )
    return doc_id


def get_agent_status() -> str:
    snap = (
        db().collection(config.COL_STATE).document(config.STATE_DOC_ID).get()
    )
    if not snap.exists:
        return "running"
    return snap.to_dict().get("status", "running")


def set_agent_status(status: str, reason: str = "") -> None:
    db().collection(config.COL_STATE).document(config.STATE_DOC_ID).set(
        {"status": status, "reason": reason, "updated_at": now_iso()}
    )


def due_tasks() -> list[dict[str, Any]]:
    """Pending tasks whose due_at is now or in the past."""
    q = (
        db()
        .collection(config.COL_TASKS)
        .where("status", "==", "pending")
        .order_by("due_at")
        .limit(50)
    )
    out = []
    nowi = now_iso()
    for d in q.stream():
        rec = d.to_dict() | {"id": d.id}
        if rec.get("due_at", "") <= nowi:
            out.append(rec)
    return out


def mark_task(task_id: str, status: str) -> None:
    db().collection(config.COL_TASKS).document(task_id).update(
        {"status": status, "completed_at": now_iso()}
    )


# --------------------------------------------------------------------------- #
# GCS (attachment storage)
# --------------------------------------------------------------------------- #
def upload_attachment(content: bytes, filename: str, content_type: str) -> str:
    """Store an attachment in GCS, return its gs:// URI."""
    blob_name = f"inbound/{dt.date.today().isoformat()}/{uuid.uuid4().hex}/{filename}"
    bucket = gcs().bucket(config.ATTACHMENTS_BUCKET)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(content, content_type=content_type)
    return f"gs://{config.ATTACHMENTS_BUCKET}/{blob_name}"


# --------------------------------------------------------------------------- #
# Resend (outbound email)
# --------------------------------------------------------------------------- #
def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
    if not config.RESEND_API_KEY:
        return {"ok": False, "error": "RESEND_API_KEY not configured"}
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
    log_message(
        channel="email",
        direction="out",
        sender=config.SENDER_EMAIL,
        recipient=to,
        subject=subject,
        body=body,
        status="sent" if ok else f"error:{resp.status_code}",
    )
    return {"ok": ok, "id": data.get("id", ""), "raw": data}


def fetch_inbound_email(email_id: str) -> dict[str, Any]:
    """Fetch a received (inbound) email's full content by id.

    Resend's email.received webhook is metadata-only; the body + attachments
    come from this endpoint. Path mirrors the `resend emails receiving get` CLI
    command — verify against a real payload when inbound DNS goes live.
    """
    if not (config.RESEND_API_KEY and email_id):
        return {}
    try:
        r = requests.get(
            f"https://api.resend.com/emails/receiving/{email_id}",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            timeout=30,
        )
        return r.json() if r.ok else {}
    except Exception:  # noqa: BLE001
        return {}


def fetch_inbound_attachment(email_id: str, attachment_id: str) -> dict[str, Any]:
    """Fetch a single inbound attachment (content base64 or download url) by id."""
    if not (config.RESEND_API_KEY and email_id and attachment_id):
        return {}
    try:
        r = requests.get(
            f"https://api.resend.com/emails/receiving/{email_id}"
            f"/attachments/{attachment_id}",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            timeout=30,
        )
        return r.json() if r.ok else {}
    except Exception:  # noqa: BLE001
        return {}


def list_inbound_attachments(email_id: str) -> list[dict[str, Any]]:
    """List a received email's attachments; each item includes a download_url."""
    if not (config.RESEND_API_KEY and email_id):
        return []
    try:
        r = requests.get(
            f"https://api.resend.com/emails/receiving/{email_id}/attachments",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            timeout=30,
        )
        if not r.ok:
            return []
        body = r.json()
        items = body.get("data", body) if isinstance(body, dict) else body
        return items if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []


def latest_inbound_id() -> str:
    """Most recent inbound email id — fallback when the webhook id isn't usable."""
    if not config.RESEND_API_KEY:
        return ""
    try:
        r = requests.get(
            "https://api.resend.com/emails/receiving",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            timeout=30,
        )
        data = (r.json().get("data", []) if r.ok else []) or []
        return data[0].get("id", "") if data else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# Agent Runtime (query the deployed ADK agent)
# --------------------------------------------------------------------------- #
def query_agent(
    user_id: str,
    session_id: str,
    message: str,
    files: list[dict[str, str]] | None = None,
) -> str:
    """Send a message (optionally with multimodal file parts) to the deployed
    Agent Runtime agent and return its text reply.

    files: list of {"uri": "gs://...", "type": "<mime>"} passed as file_data
    parts so Gemini reads the media directly from Cloud Storage.
    """
    if not config.AGENT_ENGINE_RESOURCE:
        return "(agent not deployed yet: AGENT_ENGINE_RESOURCE unset)"
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=config.PROJECT_ID, location=config.REGION)
    engine = agent_engines.get(config.AGENT_ENGINE_RESOURCE)

    if files:
        parts: list[dict[str, Any]] = [{"text": message}]
        for f in files:
            parts.append(
                {"file_data": {"file_uri": f["uri"], "mime_type": f["type"]}}
            )
        outgoing: Any = {"role": "user", "parts": parts}
    else:
        outgoing = message

    chunks: list[str] = []
    for event in engine.stream_query(
        user_id=user_id, session_id=session_id, message=outgoing
    ):
        ev = event if isinstance(event, dict) else getattr(event, "__dict__", {})
        content = ev.get("content") or {}
        for part in content.get("parts", []) if isinstance(content, dict) else []:
            text = part.get("text") if isinstance(part, dict) else None
            if text:
                chunks.append(text)
    return "".join(chunks).strip() or "(no response)"


def ensure_session(user_id: str) -> str:
    """Get or create an Agent Runtime session id for a given user (email)."""
    if not config.AGENT_ENGINE_RESOURCE:
        return "local"
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=config.PROJECT_ID, location=config.REGION)
    engine = agent_engines.get(config.AGENT_ENGINE_RESOURCE)
    # Reuse the most recent session for this user, else create one.
    try:
        sessions = engine.list_sessions(user_id=user_id)
        items = sessions.get("sessions", []) if isinstance(sessions, dict) else sessions
        if items:
            first = items[0]
            return first.get("id") if isinstance(first, dict) else first.id
    except Exception:  # noqa: BLE001
        pass
    created = engine.create_session(user_id=user_id)
    return created.get("id") if isinstance(created, dict) else created.id


def _debug_dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)[:500]
    except Exception:  # noqa: BLE001
        return str(obj)[:500]
