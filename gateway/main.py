"""autoagents gateway — Cloud Run FastAPI service.

Fronts the Agent Runtime brain with the event layer Agent Runtime can't host:
  POST /inbound/email   Resend inbound-email webhook
  POST /tasks/run       Cloud Scheduler tick (runs due tasks/reminders/followups)
  GET  /healthz         health check

Every inbound/outbound message is logged to Firestore. The agent run-state
(running/paused/stopped) is honoured here so the user can start/pause/stop it.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

import requests
from fastapi import FastAPI, Request, Response

from gateway import clients, config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")
app = FastAPI(title="autoagents-gateway")

ADMIN_HELP = (
    "Commands: !status, !pause, !resume, !stop. "
    "Or just email me an instruction."
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _as_email(value: Any) -> str:
    """Normalise a Resend from/to field (string or list) to a single address."""
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        return str(value.get("email") or value.get("address") or "").lower()
    return str(value or "").lower()


def _verify_webhook(raw: bytes, headers: dict[str, str]) -> bool:
    """Verify the Svix signature Resend attaches to webhooks."""
    if not config.RESEND_WEBHOOK_SECRET:
        log.warning("RESEND_WEBHOOK_SECRET unset — skipping signature verification")
        return True
    try:
        from svix.webhooks import Webhook

        Webhook(config.RESEND_WEBHOOK_SECRET).verify(raw, headers)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("webhook verification failed: %s", exc)
        return False


def _store_attachments(data: dict[str, Any]) -> list[dict[str, str]]:
    """Persist inbound attachments to GCS; return list of {name, uri, type}."""
    out: list[dict[str, str]] = []
    for att in data.get("attachments", []) or []:
        name = att.get("filename") or att.get("name") or "attachment"
        ctype = att.get("content_type") or att.get("contentType") or "application/octet-stream"
        content: bytes | None = None
        if att.get("content"):  # base64 inline
            try:
                content = base64.b64decode(att["content"])
            except Exception:  # noqa: BLE001
                content = None
        elif att.get("url"):  # fetch by URL
            try:
                r = requests.get(att["url"], timeout=30)
                if r.ok:
                    content = r.content
            except Exception:  # noqa: BLE001
                content = None
        if content:
            uri = clients.upload_attachment(content, name, ctype)
            out.append({"name": name, "uri": uri, "type": ctype})
    return out


def _handle_admin(sender: str, text: str) -> str | None:
    """Return a reply if `text` is an admin command from an allowlisted sender."""
    if sender not in config.ADMIN_EMAILS:
        return None
    cmd = text.strip().lower().split()[0] if text.strip() else ""
    if cmd == "!status":
        status = clients.get_agent_status()
        due = clients.due_tasks()
        return f"Status: {status}. {len(due)} task(s) due now. {ADMIN_HELP}"
    if cmd == "!pause":
        clients.set_agent_status("paused", reason=f"admin:{sender}")
        return "Paused. I'll keep logging but take no action until !resume."
    if cmd == "!resume":
        clients.set_agent_status("running", reason=f"admin:{sender}")
        return "Resumed. Back to work."
    if cmd == "!stop":
        clients.set_agent_status("stopped", reason=f"admin:{sender}")
        return "Stopped. I'll ignore non-admin input until !resume."
    return None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/inbound/email")
async def inbound_email(request: Request) -> Response:
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _verify_webhook(raw, headers):
        return Response(status_code=401, content="invalid signature")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return Response(status_code=400, content="bad json")

    etype = payload.get("type", "")
    data = payload.get("data", payload) or {}

    # Only act on inbound-email events; ack everything else (delivery receipts etc.).
    if "inbound" not in etype and "received" not in etype and not data.get("text"):
        log.info("ignoring non-inbound event: %s", etype)
        return Response(status_code=200, content="ignored")

    sender = _as_email(data.get("from"))
    subject = data.get("subject", "") or ""
    text = data.get("text") or data.get("html") or ""
    attachments = _store_attachments(data)

    session_id = ""  # set below when we query the agent
    clients.log_message(
        channel="email",
        direction="in",
        sender=sender,
        recipient=_as_email(data.get("to")) or config.SENDER_EMAIL,
        subject=subject,
        body=text,
        attachments=attachments,
        status="received",
    )

    # Admin command short-circuit.
    body_for_cmd = (subject if subject.startswith("!") else text)
    admin_reply = _handle_admin(sender, body_for_cmd)
    if admin_reply is not None:
        clients.send_email(sender, f"Re: {subject}" if subject else "autoagents", admin_reply)
        return Response(status_code=200, content="admin handled")

    # Respect run-state.
    status = clients.get_agent_status()
    if status == "stopped":
        log.info("agent stopped; ignoring inbound from %s", sender)
        return Response(status_code=200, content="stopped")
    if status == "paused":
        clients.send_email(
            sender,
            f"Re: {subject}" if subject else "autoagents",
            "I'm paused right now and will get to this once resumed.",
        )
        return Response(status_code=200, content="paused")

    # Hand to the agent.
    prompt = text
    if attachments:
        refs = "\n".join(f"- {a['name']} ({a['type']}): {a['uri']}" for a in attachments)
        prompt = f"{text}\n\n[Attachments stored in GCS]\n{refs}"

    try:
        session_id = clients.ensure_session(sender)
        reply = clients.query_agent(user_id=sender, session_id=session_id, message=prompt)
    except Exception as exc:  # noqa: BLE001
        log.exception("agent query failed")
        reply = f"(sorry, I hit an error processing that: {exc})"

    clients.send_email(sender, f"Re: {subject}" if subject else "autoagents reply", reply)
    return Response(status_code=200, content="ok")


@app.post("/tasks/run")
async def tasks_run(request: Request) -> dict[str, Any]:
    """Scheduler tick: execute any due tasks/reminders/followups."""
    if clients.get_agent_status() != "running":
        return {"ran": 0, "skipped": "agent not running"}

    ran = 0
    for task in clients.due_tasks():
        instruction = (
            f"Scheduled {task.get('type', 'task')} is due. Carry it out now: "
            f"{task.get('description', '')}"
        )
        try:
            admin = config.ADMIN_EMAILS[0] if config.ADMIN_EMAILS else "user"
            session_id = clients.ensure_session(admin)
            clients.query_agent(user_id=admin, session_id=session_id, message=instruction)
            clients.mark_task(task["id"], "done")
            ran += 1
        except Exception:  # noqa: BLE001
            log.exception("task %s failed", task.get("id"))
            clients.mark_task(task["id"], "error")
    return {"ran": ran}
