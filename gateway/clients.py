"""External clients + helpers for the gateway: Firestore, GCS, Resend, Agent Runtime."""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from typing import Any

import requests
from google.cloud import firestore, storage

from gateway import config

log = logging.getLogger("gateway.clients")

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
    tenant_id: str = "",
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
            "tenant_id": tenant_id,
            "ts": now_iso(),
        }
    )
    return doc_id


def get_agent_status(tenant_id: str) -> str:
    """Per-tenant run state ('running'/'paused'/'stopped'). Defaults to running."""
    snap = db().collection(config.COL_STATE).document(tenant_id).get()
    if not snap.exists:
        return "running"
    return (snap.to_dict() or {}).get("status", "running")


def set_agent_status(tenant_id: str, status: str, reason: str = "") -> None:
    db().collection(config.COL_STATE).document(tenant_id).set(
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


def ensure_tenant_corpus(tenant_id: str) -> str:
    """Ensure a tenant has its own RAG corpus; create one on first need.

    Per-tenant corpora give each agent a private long-term document store
    (physical isolation). Stores the corpus resource name on the tenant doc and
    returns it. Best-effort: on failure the tenant simply has no doc store yet.
    """
    ref = db().collection(config.COL_TENANTS).document(tenant_id)
    existing = (ref.get().to_dict() or {}).get("rag_corpus")
    if existing:
        return existing
    try:
        import vertexai
        from vertexai import rag
        from vertexai.rag.utils import resources as r

        vertexai.init(project=config.PROJECT_ID, location=config.RAG_LOCATION)
        try:
            cfg = f"projects/{config.PROJECT_ID}/locations/{config.RAG_LOCATION}/ragEngineConfig"
            rag.update_rag_engine_config(
                rag_engine_config=rag.RagEngineConfig(
                    name=cfg,
                    rag_managed_db_config=rag.RagManagedDbConfig(tier=r.Basic()),
                )
            )
        except Exception:  # noqa: BLE001 - already-basic is fine
            pass
        corpus = rag.create_corpus(display_name=f"autoagents-{tenant_id}")
        ref.set({"rag_corpus": corpus.name}, merge=True)
        return corpus.name
    except Exception:  # noqa: BLE001 - degrade gracefully (no doc store yet)
        log.exception("ensure_tenant_corpus failed for %s", tenant_id)
        return ""


def record_usage(tenant_id: str, model: str, usage: dict[str, int]) -> None:
    """Record one agent turn's token usage for per-tenant analytics (best-effort)."""
    if usage.get("total", 0) <= 0:
        return
    try:
        db().collection(config.COL_USAGE).document(uuid.uuid4().hex).set(
            {
                "tenant_id": tenant_id,
                "model": model or "",
                "prompt_tokens": int(usage.get("prompt", 0)),
                "output_tokens": int(usage.get("output", 0)),
                "thoughts_tokens": int(usage.get("thoughts", 0)),
                "total_tokens": int(usage.get("total", 0)),
                "ts": now_iso(),
            }
        )
    except Exception:  # noqa: BLE001 - analytics must never break a reply
        log.exception("record_usage failed for %s", tenant_id)


def latest_outbound_to(tenant_id: str, channel: str, contact: str) -> str:
    """ISO ts of the most recent outbound message this tenant sent to ``contact``.

    Used to detect that the agent re-sent to a third party (which reopens their
    reply window). Empty string if the tenant never messaged that contact.
    """
    cl = contact.strip().lower()
    best = ""
    for d in (
        db().collection(config.COL_MESSAGES).where("tenant_id", "==", tenant_id).stream()
    ):
        r = d.to_dict()
        if (
            r.get("direction") == "out"
            and r.get("channel") == channel
            and str(r.get("to", "")).strip().lower() == cl
        ):
            ts = r.get("ts", "")
            if ts > best:
                best = ts
    return best


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


def send_whatsapp(to: str, text: str) -> dict[str, Any]:
    """Send a WhatsApp message via the Baileys bridge and log it."""
    if not (config.WHATSAPP_BRIDGE_URL and config.WHATSAPP_BRIDGE_SECRET):
        return {"ok": False, "error": "whatsapp bridge not configured"}
    ok = False
    data: dict[str, Any] = {}
    try:
        r = requests.post(
            config.WHATSAPP_BRIDGE_URL.rstrip("/") + "/send",
            headers={"X-WA-Secret": config.WHATSAPP_BRIDGE_SECRET},
            json={"to": to, "text": text},
            timeout=30,
        )
        ok = r.ok
        data = r.json() if r.content else {}
    except Exception as exc:  # noqa: BLE001
        data = {"error": str(exc)}
    try:
        log_message(
            channel="whatsapp",
            direction="out",
            sender="bridge",
            recipient=to,
            body=text,
            status="sent" if ok else "error",
        )
    except Exception:  # noqa: BLE001 - logging must not mask a real send
        pass
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
def _run_async(coro: Any) -> Any:
    """Run a coroutine to completion whether or not an event loop is active.

    The engine only exposes async memory methods, but the gateway's FastAPI
    handlers run inside an event loop where ``asyncio.run()`` raises "cannot be
    called from a running event loop". When a loop is already running, execute
    the coroutine in a short-lived worker thread (which has no loop).
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _memory_facts(engine: Any, user_id: str, query: str) -> list[str]:
    """Retrieve a user's long-term memories via the engine's Memory Bank API.

    We orchestrate memory here (not in the agent) because the deployed runtime's
    native Memory Bank wiring is unavailable, but the engine's memory API works.
    Scoped by user_id, so it isolates per user (and per tenant).
    """
    try:
        res = _run_async(engine.async_search_memory(user_id=user_id, query=query))
    except Exception:  # noqa: BLE001
        return []
    mems = res.get("memories", []) if isinstance(res, dict) else []
    facts: list[str] = []
    for m in mems:
        c = m.get("content", {}) if isinstance(m, dict) else {}
        for p in c.get("parts", []) if isinstance(c, dict) else []:
            t = p.get("text") if isinstance(p, dict) else None
            if t:
                facts.append(t)
    return facts


def _store_memory(engine: Any, user_id: str, session_id: str) -> bool:
    """Persist a session to the user's long-term memory. Returns True on success.

    Called after every turn (best-effort) and again before a session is rotated
    out — there the return value gates the rotation so memory is never dropped.
    """
    try:
        sess = engine.get_session(user_id=user_id, session_id=session_id)
        _run_async(engine.async_add_session_to_memory(session=sess))
        return True
    except Exception:  # noqa: BLE001
        return False


def _idle_expired(last_at: str, now: dt.datetime) -> bool:
    """True if `last_at` (ISO) is older than the session idle window."""
    try:
        prev = dt.datetime.fromisoformat(last_at)
    except Exception:  # noqa: BLE001 - unparseable → don't force a rotation
        return False
    return (now - prev) > dt.timedelta(hours=config.SESSION_IDLE_HOURS)


def _latest_matching_session(engine: Any, user_id: str, want: str | None) -> str | None:
    """The user's most recent session id, if its state matches `want` (else None).

    Used once to adopt a pre-existing session into the pointer (graceful first run).
    """
    try:
        sessions = engine.list_sessions(user_id=user_id)
        items = sessions.get("sessions", []) if isinstance(sessions, dict) else sessions
        if not items:
            return None
        first = items[0]
        sid = first.get("id") if isinstance(first, dict) else first.id
        if not want:
            return sid
        got = engine.get_session(user_id=user_id, session_id=sid)
        st = (got.get("state") if isinstance(got, dict) else getattr(got, "state", {})) or {}
        return sid if st.get("tenant_id") == want else None
    except Exception:  # noqa: BLE001
        return None


def query_agent(
    user_id: str,
    session_id: str,
    message: str,
    files: list[dict[str, str]] | None = None,
) -> str:
    """Send a message (optionally with multimodal file parts) to the deployed
    Agent Runtime agent and return its text reply. Retrieves long-term memory for
    the user and injects it, then stores the turn back to memory.

    files: list of {"uri": "gs://...", "type": "<mime>"} passed as file_data
    parts so Gemini reads the media directly from Cloud Storage.
    """
    if not config.AGENT_ENGINE_RESOURCE:
        return "(agent not deployed yet: AGENT_ENGINE_RESOURCE unset)"
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=config.PROJECT_ID, location=config.REGION)
    engine = agent_engines.get(config.AGENT_ENGINE_RESOURCE)

    # Inject long-term memories (per user) as context.
    facts = _memory_facts(engine, user_id, message)
    text = message
    if facts:
        text = (
            "Known facts about this user (from your long-term memory):\n"
            + "\n".join(f"- {f}" for f in facts[:10])
            + "\n\n"
            + message
        )

    if files:
        parts: list[dict[str, Any]] = [{"text": text}]
        for f in files:
            parts.append(
                {"file_data": {"file_uri": f["uri"], "mime_type": f["type"]}}
            )
        outgoing: Any = {"role": "user", "parts": parts}
    else:
        outgoing = text

    chunks: list[str] = []
    usage = {"prompt": 0, "output": 0, "thoughts": 0, "total": 0}
    model_version = ""
    for event in engine.stream_query(
        user_id=user_id, session_id=session_id, message=outgoing
    ):
        ev = event if isinstance(event, dict) else getattr(event, "__dict__", {})
        content = ev.get("content") or {}
        for part in content.get("parts", []) if isinstance(content, dict) else []:
            text_part = part.get("text") if isinstance(part, dict) else None
            if text_part:
                chunks.append(text_part)
        # Accumulate token usage across every LLM call in this turn (tool steps
        # produce multiple events, each with its own usage_metadata).
        um = ev.get("usage_metadata") or ev.get("usageMetadata")
        if isinstance(um, dict):
            usage["prompt"] += int(um.get("prompt_token_count", 0) or 0)
            usage["output"] += int(um.get("candidates_token_count", 0) or 0)
            usage["thoughts"] += int(um.get("thoughts_token_count", 0) or 0)
            usage["total"] += int(um.get("total_token_count", 0) or 0)
        model_version = model_version or ev.get("model_version", "") or ""
    reply = "".join(chunks).strip() or "(no response)"

    record_usage(user_id, model_version, usage)
    _store_memory(engine, user_id, session_id)
    return reply


def ensure_session(user_id: str, state: dict[str, Any] | None = None) -> str:
    """Get the user's active Agent Runtime session, rotating after idle time.

    A per-tenant pointer in Firestore (``agent_sessions/<user_id>`` =
    ``{session_id, last_at}``) tracks the live session and its last activity:

    - active (last used within ``SESSION_IDLE_HOURS``) → reuse it, bump ``last_at``;
    - idle longer than the window → **flush the old session to long-term memory
      first**; only if that succeeds do we create a fresh session (otherwise we
      keep the old one so nothing is lost);
    - no pointer yet → adopt the user's latest matching session, else create one.

    ``state`` (``{tenant_id, rag_corpus}``) is written onto a newly created session
    so the agent's tools can read it via ``ToolContext.state``.
    """
    if not config.AGENT_ENGINE_RESOURCE:
        return "local"
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=config.PROJECT_ID, location=config.REGION)
    engine = agent_engines.get(config.AGENT_ENGINE_RESOURCE)
    want = (state or {}).get("tenant_id")
    now = dt.datetime.now(dt.UTC)
    nowi = now.isoformat()
    pref = db().collection(config.COL_SESSIONS).document(user_id)
    ptr = pref.get().to_dict() or {}
    sid = ptr.get("session_id")
    last_at = ptr.get("last_at")

    if sid and last_at and not _idle_expired(last_at, now):
        # Still active — reuse and record this activity.
        pref.set({"last_at": nowi}, merge=True)
        return sid

    if sid and last_at:
        # Idle past the window: guarantee memory is captured before rotating.
        if not _store_memory(engine, user_id, sid):
            log.warning("session %s memory flush failed; keeping it (no rotate)", sid)
            pref.set({"last_at": nowi}, merge=True)
            return sid
        # flushed → fall through and create a fresh session

    if not sid:
        # First run for this tenant: adopt an existing matching session if any.
        adopted = _latest_matching_session(engine, user_id, want)
        if adopted:
            pref.set(
                {"session_id": adopted, "last_at": nowi, "tenant_id": user_id}, merge=True
            )
            return adopted

    created = engine.create_session(user_id=user_id, state=state or {})
    new_sid = created.get("id") if isinstance(created, dict) else created.id
    pref.set({"session_id": new_sid, "last_at": nowi, "tenant_id": user_id})
    return new_sid


def _debug_dump(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)[:500]
    except Exception:  # noqa: BLE001
        return str(obj)[:500]
