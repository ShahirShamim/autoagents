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
import html
import json
import logging
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from gateway import clients, config, tenancy

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")
app = FastAPI(title="autoagents-gateway")

ADMIN_HELP = (
    "Commands: !status, !pause, !resume, !stop. "
    "Or just email me an instruction."
)

WELCOME_EMAIL = (
    "You're connected to your autoagents assistant. Reply to this address any time "
    "with what you need — send a message, ask me to email or WhatsApp someone, set a "
    "reminder, or share a file. I'll handle the rest."
)
WELCOME_WA = (
    "You're connected to your autoagents assistant. Message me any time with what you "
    "need and I'll handle it."
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


# Mime types Gemini can read directly as multimodal input.
def _model_supported(mime: str) -> bool:
    return mime == "application/pdf" or mime.startswith(
        ("image/", "audio/", "video/", "text/")
    )


def _store_attachments(email_id: str) -> list[dict[str, str]]:
    """Fetch the inbound email's attachments via Resend (list endpoint gives a
    download_url per file), download the bytes, store in GCS, and return
    [{name, uri, type}].
    """
    out: list[dict[str, str]] = []
    if not email_id:
        return out
    for a in clients.list_inbound_attachments(email_id):
        name = a.get("filename") or a.get("name") or "attachment"
        ctype = (
            a.get("content_type") or a.get("contentType") or "application/octet-stream"
        )
        url = a.get("download_url") or a.get("url")
        content: bytes | None = None
        if url:
            try:
                r = requests.get(url, timeout=60)
                if r.ok:
                    content = r.content
            except Exception:  # noqa: BLE001
                content = None
        elif a.get("content"):
            try:
                content = base64.b64decode(a["content"])
            except Exception:  # noqa: BLE001
                content = None
        if content:
            uri = clients.upload_attachment(content, name, ctype)
            out.append({"name": name, "uri": uri, "type": ctype})
    return out


def _admin_command(text: str, tenant_id: str, who: str = "owner") -> str | None:
    """Parse a per-tenant control command. Caller must confirm `who` is the
    tenant's own (identity-resolved) owner before calling — third parties routed
    via a thread (Phase 4) must never reach this.
    """
    cmd = text.strip().lower().split()[0] if text.strip() else ""
    if cmd == "!status":
        status = clients.get_agent_status(tenant_id)
        due = clients.due_tasks()
        return f"Status: {status}. {len(due)} task(s) due now. {ADMIN_HELP}"
    if cmd == "!pause":
        clients.set_agent_status(tenant_id, "paused", reason=who)
        return "Paused. I'll keep logging but take no action until !resume."
    if cmd == "!resume":
        clients.set_agent_status(tenant_id, "running", reason=who)
        return "Resumed. Back to work."
    if cmd == "!stop":
        clients.set_agent_status(tenant_id, "stopped", reason=who)
        return "Stopped. I'll ignore non-admin input until !resume."
    return None


def _route_sender(channel: str, sender: str) -> tuple[str | None, str]:
    """Resolve an inbound sender to (tenant_id, disposition).

    disposition is one of:
      "active"  → registered, active tenant; process normally
      "onboard" → registered but pending; activate + welcome, then process
      "reject"  → unknown sender or disabled tenant; log + drop, no agent call

    Phase 2 routes by **registered identity only**. Third-party reply routing via
    the tagged address / open threads is added in Phase 4.
    """
    tenant_id = tenancy.resolve_tenant(channel, sender)
    if not tenant_id:
        return None, "reject"
    status = (tenancy.tenant_config(tenant_id) or {}).get("status", "active")
    if status == "pending":
        return tenant_id, "onboard"
    if status != "active":
        return tenant_id, "reject"
    return tenant_id, "active"


def _wa_is_owner(tenant_id: str, pn: str, lid: str, frm: str) -> bool:
    """True if this inbound is the tenant OWNER instructing their assistant.

    Each tenant links their own WhatsApp account, so inbound on that socket is
    either the owner (messaging from their registered personal number) or a
    third-party contact. We match the sender's phone/LID against this tenant's
    own registered identities; anything else is a third party.
    """
    for cand in (pn, lid, tenancy.normalize_phone(frm)):
        if cand and tenancy.resolve_tenant("whatsapp", cand) == tenant_id:
            return True
    return False


def _session_state(tenant_id: str) -> dict[str, str]:
    """Session state the agent's tools read via ToolContext to scope per tenant."""
    tcfg = tenancy.tenant_config(tenant_id) or {}
    return {"tenant_id": tenant_id, "rag_corpus": tcfg.get("rag_corpus", "")}


def _framed(tenant_id: str, prompt: str) -> str:
    """Prepend the tenant's operator-authored standing instructions to a prompt.

    Read fresh from the tenant doc each turn (not session state) so edits in the
    admin panel take effect on the very next message — no session rotation wait.
    No-op when no context is set.
    """
    ctx = tenancy.agent_context(tenant_id)
    if not ctx:
        return prompt
    return (
        "Standing instructions for this user — always honour these:\n"
        f"{ctx}\n\n----------\n\n{prompt}"
    )


def _to_addresses(*objs: dict[str, Any]) -> list[str]:
    """All recipient addresses across the inbound payload variants (str/list/dict)."""
    out: list[str] = []
    for o in objs:
        v = o.get("to") if isinstance(o, dict) else None
        if isinstance(v, str):
            out.append(v.lower())
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    out.append(str(x.get("email") or x.get("address") or "").lower())
                else:
                    out.append(str(x).lower())
    return [a for a in out if a]


def _tagged_tenant(addrs: list[str]) -> str | None:
    """First recipient that is a reply-routable tenant tag, else None."""
    for a in addrs:
        t = tenancy.parse_tagged_tenant(a)
        if t:
            return t
    return None


def _thread_reply_email(tenant_id: str, sender: str, subject: str, text: str) -> None:
    """A third party replied to a tagged address: enforce the access window, then
    have the tenant's agent read it and relay a summary to the tenant owner."""
    last_out = clients.latest_outbound_to(tenant_id, "email", sender)
    disp, courtesy = tenancy.apply_thread_ttl(tenant_id, "email", sender, last_out)
    if disp == "blocked":
        if courtesy:
            clients.send_email(
                tenant_id,
                sender,
                f"Re: {subject}" if subject else "autoagents",
                "This conversation has now closed. Thanks for your message.",
            )
        log.info("thread reply from %s blocked (expired) for %s", sender, tenant_id)
        return

    prompt = (
        f"A reply just came in from {sender} on an email thread you started on the "
        f"user's behalf"
        + (f" (subject: {subject})" if subject else "")
        + f".\n\nTheir message:\n{text}\n\n"
        "Summarise this reply for the user and note any action it calls for. Do not "
        "email this third party again unless the user asks."
    )
    try:
        session_id = clients.ensure_session(tenant_id, state=_session_state(tenant_id))
        summary = clients.query_agent(tenant_id, session_id, _framed(tenant_id, prompt))
    except Exception as exc:  # noqa: BLE001
        log.exception("thread reply agent query failed")
        clients.record_alert(
            "thread_reply_error",
            f"failed reading reply from {sender}: {exc}",
            tenant_id=tenant_id,
            severity="error",
        )
        summary = f"(reply from {sender}, but I hit an error reading it: {exc})\n\n{text}"

    owner = tenancy.primary_email(tenant_id)
    if owner:
        clients.send_email(
            tenant_id,
            owner,
            f"Reply from {sender}" + (f" re: {subject}" if subject else ""),
            summary,
        )


def _thread_reply_whatsapp(tenant_id: str, contact: str, text: str) -> None:
    """A third party replied on WhatsApp to a thread the tenant's agent started:
    enforce the access window, have the agent read it, and relay a summary to the
    tenant owner over WhatsApp."""
    last_out = clients.latest_outbound_to(tenant_id, "whatsapp", contact)
    disp, courtesy = tenancy.apply_thread_ttl(tenant_id, "whatsapp", contact, last_out)
    if disp == "blocked":
        if courtesy:
            clients.send_whatsapp(
                tenant_id, contact, "This conversation has now closed. Thanks for your message."
            )
        log.info("wa thread reply from %s blocked (expired) for %s", contact, tenant_id)
        return

    prompt = (
        f"A reply just came in on WhatsApp from {contact}, on a thread you started "
        f"on the user's behalf.\n\nTheir message:\n{text}\n\n"
        "Summarise this reply for the user and note any action it calls for. Do not "
        "message this third party again unless the user asks."
    )
    try:
        session_id = clients.ensure_session(tenant_id, state=_session_state(tenant_id))
        summary = clients.query_agent(tenant_id, session_id, _framed(tenant_id, prompt))
    except Exception as exc:  # noqa: BLE001
        log.exception("wa thread reply agent query failed")
        clients.record_alert(
            "thread_reply_error",
            f"failed reading wa reply from {contact}: {exc}",
            tenant_id=tenant_id,
            severity="error",
        )
        summary = f"(reply from {contact}, but I hit an error reading it: {exc})\n\n{text}"

    owner = tenancy.primary_whatsapp(tenant_id)
    if owner:
        clients.send_whatsapp(tenant_id, owner, summary)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
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

    # Resend's email.received webhook carries metadata only — fetch the full
    # email (body + attachments) by id. Verify the retrieve path against a real
    # payload once inbound DNS is live.
    email_id = data.get("email_id") or data.get("id") or ""
    full = clients.fetch_inbound_email(email_id) if email_id else {}
    if "received" in etype and not full.get("id"):
        # Webhook id wasn't a retrievable inbound id — fall back to most recent.
        email_id = clients.latest_inbound_id()
        full = clients.fetch_inbound_email(email_id) if email_id else {}
    src = full or data
    log.info(
        "inbound email_id=%s full=%s payload_keys=%s data_keys=%s",
        email_id, bool(full.get("id")), list(payload.keys()), list(data.keys()),
    )

    sender = _as_email(src.get("from") or data.get("from"))
    subject = src.get("subject") or data.get("subject") or ""
    text = src.get("text") or src.get("html") or data.get("text") or ""
    attachments = _store_attachments(email_id)

    tenant_id, disp = _route_sender("email", sender)

    # Third-party reply to a tagged address (assistant+<tenant>@) → thread routing.
    # Checked before reject: the sender isn't a registered identity, but the tag
    # proves which tenant's agent initiated the conversation.
    if disp == "reject":
        tt = _tagged_tenant(_to_addresses(src, data))
        if tt:
            clients.log_message(
                channel="email", direction="in", sender=sender,
                recipient=config.SENDER_EMAIL, subject=subject, body=text,
                attachments=attachments, status="thread_reply", tenant_id=tt,
            )
            _thread_reply_email(tt, sender, subject, text)
            return Response(status_code=200, content="thread reply")

    # Audit every inbound, including rejects.
    clients.log_message(
        channel="email",
        direction="in",
        sender=sender,
        recipient=_as_email(data.get("to")) or config.SENDER_EMAIL,
        subject=subject,
        body=text,
        attachments=attachments,
        status="received" if tenant_id else "rejected_unknown",
        tenant_id=tenant_id or "",
    )

    if disp == "reject":
        log.info("rejecting unknown/inactive email sender %s", sender)
        return Response(status_code=200, content="unknown sender")

    if disp == "onboard":
        tenancy.activate_tenant(tenant_id)
        clients.ensure_tenant_corpus(tenant_id)  # give the new agent a doc store
        clients.send_email(tenant_id, sender, "Welcome to autoagents", WELCOME_EMAIL)

    # Owner control command (sender resolved via their own registered identity).
    body_for_cmd = subject if subject.startswith("!") else text
    if body_for_cmd.strip().startswith("!"):
        admin_reply = _admin_command(body_for_cmd, tenant_id, who=f"email:{sender}")
        if admin_reply is not None:
            clients.send_email(
                tenant_id, sender, f"Re: {subject}" if subject else "autoagents", admin_reply
            )
            return Response(status_code=200, content="admin handled")

    # Per-tenant run-state.
    status = clients.get_agent_status(tenant_id)
    if status == "stopped":
        log.info("tenant %s stopped; ignoring inbound from %s", tenant_id, sender)
        return Response(status_code=200, content="stopped")
    if status == "paused":
        clients.send_email(
            tenant_id,
            sender,
            f"Re: {subject}" if subject else "autoagents",
            "I'm paused right now and will get to this once resumed.",
        )
        return Response(status_code=200, content="paused")

    # Hand to the agent. Media Gemini can read goes in as file_data parts;
    # anything else is mentioned by reference so the agent at least knows of it.
    files = [
        {"uri": a["uri"], "type": a["type"]}
        for a in attachments
        if _model_supported(a["type"])
    ]
    prompt = text or "(no text body)"
    other = [a for a in attachments if not _model_supported(a["type"])]
    if other:
        refs = "\n".join(f"- {a['name']} ({a['type']}): {a['uri']}" for a in other)
        prompt = f"{prompt}\n\n[Attachments stored but not directly readable]\n{refs}"

    try:
        session_id = clients.ensure_session(tenant_id, state=_session_state(tenant_id))
        reply = clients.query_agent(
            user_id=tenant_id, session_id=session_id, message=_framed(tenant_id, prompt), files=files
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("agent query failed")
        clients.record_alert(
            "agent_error", f"email turn failed: {exc}", tenant_id=tenant_id, severity="error"
        )
        reply = f"(sorry, I hit an error processing that: {exc})"

    clients.send_email(tenant_id, sender, f"Re: {subject}" if subject else "autoagents reply", reply)
    return Response(status_code=200, content="ok")


@app.post("/inbound/whatsapp")
async def inbound_whatsapp(request: Request) -> Response:
    """Inbound WhatsApp message from the Baileys bridge."""
    if (
        not config.WHATSAPP_BRIDGE_SECRET
        or request.headers.get("x-wa-secret") != config.WHATSAPP_BRIDGE_SECRET
    ):
        raise HTTPException(status_code=401, detail="unauthorized")
    payload = await request.json()
    # The message arrived on this tenant's own linked WhatsApp socket, so the
    # bridge tells us the tenant directly — routing is unambiguous.
    tenant_id = str(payload.get("tenant", ""))
    sender = str(payload.get("from", ""))
    pn = tenancy.normalize_phone(str(payload.get("pn", "")))
    lid = tenancy.normalize_phone(str(payload.get("lid", "")))
    text = payload.get("text") or ""
    media = payload.get("media")

    if not tenant_id or not tenancy.tenant_config(tenant_id):
        log.warning("wa inbound for unknown tenant %r", tenant_id)
        return Response(status_code=200, content="unknown tenant")

    attachments: list[dict[str, str]] = []
    if isinstance(media, dict) and media.get("uri"):
        attachments = [
            {
                "name": "wa-media",
                "uri": media["uri"],
                "type": media.get("type", "application/octet-stream"),
            }
        ]

    is_owner = _wa_is_owner(tenant_id, pn, lid, sender)
    reply_to = pn or sender
    log.info(
        "wa inbound tenant=%s from=%s pn=%s lid=%s owner=%s",
        tenant_id, sender, pn or "-", lid or "-", is_owner,
    )

    # A non-owner messaged this tenant's number. Relay it ONLY if it's a genuine
    # reply — i.e. the tenant's agent actually messaged this contact before.
    # Unsolicited inbound (random numbers, WhatsApp status/broadcasts) is dropped,
    # never forwarded to the owner.
    if not is_owner:
        contact = pn or lid or tenancy.normalize_phone(sender)
        if not (contact and clients.latest_outbound_to(tenant_id, "whatsapp", contact)):
            clients.log_message(
                channel="whatsapp", direction="in", sender=sender, recipient=tenant_id,
                body=text, attachments=attachments, status="rejected_unsolicited",
                tenant_id=tenant_id,
            )
            log.info(
                "dropping unsolicited whatsapp from %s (pn=%s lid=%s) on %s",
                sender, pn or "-", lid or "-", tenant_id,
            )
            return Response(status_code=200, content="unsolicited")
        clients.log_message(
            channel="whatsapp", direction="in", sender=sender, recipient=tenant_id,
            body=text, attachments=attachments, status="thread_reply", tenant_id=tenant_id,
        )
        _thread_reply_whatsapp(tenant_id, contact, text)
        return Response(status_code=200, content="thread reply")

    # Owner instructing their assistant.
    clients.log_message(
        channel="whatsapp", direction="in", sender=sender, recipient=tenant_id,
        body=text, attachments=attachments, status="received", tenant_id=tenant_id,
    )

    if (tenancy.tenant_config(tenant_id) or {}).get("status") == "pending":
        tenancy.activate_tenant(tenant_id)
        clients.ensure_tenant_corpus(tenant_id)  # give the new agent a doc store
        clients.send_whatsapp(tenant_id, reply_to, WELCOME_WA)

    if text.strip().startswith("!"):
        cmd_reply = _admin_command(text, tenant_id, who=f"wa:{sender}")
        if cmd_reply is not None:
            clients.send_whatsapp(tenant_id, reply_to, cmd_reply)
            return Response(status_code=200, content="admin")

    status = clients.get_agent_status(tenant_id)
    if status == "stopped":
        return Response(status_code=200, content="stopped")
    if status == "paused":
        clients.send_whatsapp(
            tenant_id, reply_to, "I'm paused right now and will get to this once resumed."
        )
        return Response(status_code=200, content="paused")

    files = [
        {"uri": a["uri"], "type": a["type"]}
        for a in attachments
        if _model_supported(a["type"])
    ]
    # Show a "typing…" indicator to the owner while the agent works (cleared when
    # the reply is sent). Best-effort — never blocks the turn.
    clients.wa_typing(tenant_id, reply_to)
    try:
        session_id = clients.ensure_session(tenant_id, state=_session_state(tenant_id))
        reply = clients.query_agent(
            user_id=tenant_id, session_id=session_id,
            message=_framed(tenant_id, text or "(no text)"), files=files
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("whatsapp agent query failed")
        clients.record_alert(
            "agent_error", f"whatsapp turn failed: {exc}", tenant_id=tenant_id, severity="error"
        )
        reply = f"(sorry, I hit an error: {exc})"
    clients.send_whatsapp(tenant_id, reply_to, reply)
    return Response(status_code=200, content="ok")


# --------------------------------------------------------------------------- #
# Self-service WhatsApp linking (magic link → QR pairing on the bridge)
# --------------------------------------------------------------------------- #
def _link_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.LINK_SECRET or "unset", salt="aa-walink")


def _make_link_token(tenant_id: str) -> str:
    return _link_serializer().dumps(tenant_id)


def _load_link_token(token: str) -> str | None:
    if not (config.LINK_SECRET and token):
        return None
    try:
        return _link_serializer().loads(token, max_age=config.LINK_MAX_AGE_HOURS * 3600)
    except (BadSignature, SignatureExpired):
        return None


_LINK_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<title>Link WhatsApp · autoagents</title>
<style>
 body{font-family:system-ui,-apple-system,sans-serif;max-width:460px;margin:2rem auto;
   padding:0 1.2rem;color:#1d1d1f;text-align:center;line-height:1.5}
 h1{font-size:1.4rem;font-weight:600}.muted{color:#86868b;font-size:.95rem}
 #qr img{width:280px;height:280px;border:1px solid #eaecef;border-radius:12px;margin:1rem 0}
 .ok{color:#1a7f37;font-weight:600;font-size:1.1rem}
 .num{font-family:ui-monospace,monospace;font-size:1.05rem}
 button{margin-top:1rem;padding:.6rem 1.2rem;border:1px solid #d0d7de;border-radius:24px;
   background:#f6f8fa;cursor:pointer;font-size:.95rem}button:hover{background:#eef1f4}
 .spin{color:#86868b}
 .warn{background:#fff8e7;border:1px solid #ffe082;border-radius:12px;padding:.85rem 1rem;
   text-align:left;font-size:.92rem;margin:1rem 0;line-height:1.55}
 .steps{text-align:left;margin:1.1rem 0;padding-left:1.3rem;font-size:.95rem}
 .steps li{margin:.4rem 0}
</style></head><body>
<h1>Connect __NAME__'s WhatsApp</h1>
<div class=warn>📱 <b>Use a SECOND WhatsApp account — not your personal one.</b>
This number becomes your assistant's line: it will send and reply to messages on your
behalf, so it must be separate from the WhatsApp you use yourself. A spare SIM, an old
phone, or the <b>WhatsApp Business</b> app signed in with a different number all work.</div>
<p class=muted>On the phone or app holding that second account:</p>
<ol class=steps>
  <li>Open <b>WhatsApp</b> (or WhatsApp Business).</li>
  <li>Go to <b>Settings → Linked Devices</b> &nbsp;<span class=muted>(Android: ⋮ Menu → Linked devices)</span>.</li>
  <li>Tap <b>Link a device</b>.</li>
  <li>Point the camera at the QR code below.</li>
</ol>
<div id=area><p class=spin>Starting…</p></div>
<script>
const T="__TOKEN__";
async function tick(){
  try{
    const s=await (await fetch(`/link/${T}/status`)).json();
    if(s.connected){
      document.getElementById('area').innerHTML=
        `<p class=ok>✓ Connected</p><p class=num>${s.number||''}</p>`+
        `<button onclick="unlink()">Change number</button>`;
      return setTimeout(tick,6000);
    }
    const q=await (await fetch(`/link/${T}/qr`)).json();
    if(q.connected){ return tick(); }
    document.getElementById('area').innerHTML = q.qr
      ? `<div id=qr><img src="${q.qr}"></div><p class=muted>Waiting for scan…</p>`
      : `<p class=spin>Generating code…</p>`;
  }catch(e){ document.getElementById('area').innerHTML='<p class=muted>Connection issue, retrying…</p>'; }
  setTimeout(tick,3000);
}
async function unlink(){
  if(!confirm('Unlink this WhatsApp number?'))return;
  document.getElementById('area').innerHTML='<p class=spin>Unlinking…</p>';
  await fetch(`/link/${T}/unlink`,{method:'POST'});
  setTimeout(tick,1500);
}
tick();
</script></body></html>"""


@app.get("/link", response_class=HTMLResponse)
def wa_link_page(token: str = "") -> Response:
    tid = _load_link_token(token)
    cfg = tenancy.tenant_config(tid) if tid else None
    if not cfg:
        return HTMLResponse(
            "<p style='font-family:sans-serif;text-align:center;margin-top:3rem'>"
            "This link is invalid or has expired.</p>",
            status_code=400,
        )
    page = _LINK_HTML.replace("__TOKEN__", html.escape(token)).replace(
        "__NAME__", html.escape(str(cfg.get("name") or tid))
    )
    return HTMLResponse(page)


@app.get("/link/{token}/status")
def wa_link_status(token: str) -> dict[str, Any]:
    tid = _load_link_token(token)
    if not tid:
        return {"error": "invalid link"}
    s = clients.wa_session_status(tid)
    if s.get("connected") and s.get("number"):
        clients.record_wa_link(tid, str(s["number"]))
    return {"connected": bool(s.get("connected")), "number": s.get("number", "")}


@app.get("/link/{token}/qr")
def wa_link_qr(token: str) -> dict[str, Any]:
    tid = _load_link_token(token)
    if not tid:
        return {"error": "invalid link"}
    clients.wa_session_start(tid)  # ensure the session is running / pairing
    q = clients.wa_session_qr(tid)
    if q.get("connected") and q.get("number"):
        clients.record_wa_link(tid, str(q["number"]))
    return {
        "connected": bool(q.get("connected")),
        "qr": q.get("qr"),
        "number": q.get("number", ""),
    }


@app.post("/link/{token}/unlink")
def wa_link_unlink(token: str) -> dict[str, Any]:
    tid = _load_link_token(token)
    if not tid:
        return {"error": "invalid link"}
    clients.wa_session_logout(tid)
    clients.clear_wa_link(tid)
    return {"ok": True}


@app.post("/internal/wa-link/{tenant_id}")
async def internal_wa_link(request: Request, tenant_id: str) -> dict[str, Any]:
    """Token-gated: mint a magic link for a tenant and email it to them.

    Called by the admin "Send WhatsApp link" button. Returns the link too.
    """
    if config.TASKS_TOKEN and request.headers.get("x-tasks-token") != config.TASKS_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not tenancy.tenant_config(tenant_id):
        raise HTTPException(status_code=404, detail="unknown tenant")
    token = _make_link_token(tenant_id)
    link = f"{config.GATEWAY_PUBLIC_URL.rstrip('/')}/link?token={token}"
    email = tenancy.primary_email(tenant_id)
    emailed = False
    if email:
        res = clients.send_email(
            tenant_id,
            email,
            "Link your WhatsApp to autoagents",
            "Open this link on your phone to connect your assistant's WhatsApp by "
            "scanning a QR code:\n\n" + link + "\n\nImportant: you'll need a SECOND "
            "WhatsApp account for this — not your personal number. That second number "
            "becomes your assistant's line. A spare SIM, an old phone, or the WhatsApp "
            "Business app all work; the page walks you through scanning the code.\n\n"
            "The link is private to you and expires 24 hours after it was sent. Need a "
            "new one? Ask for the link again.",
        )
        emailed = bool(res.get("ok"))
    return {"ok": True, "link": link, "emailed": emailed, "to": email}


@app.post("/internal/ensure-corpus/{tenant_id}")
async def internal_ensure_corpus(request: Request, tenant_id: str) -> dict[str, Any]:
    """Token-gated: provision (or report) a tenant's RAG corpus. Used to backfill
    corpora and to surface provisioning errors."""
    if config.TASKS_TOKEN and request.headers.get("x-tasks-token") != config.TASKS_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
    return {"tenant_id": tenant_id, "corpus": clients.ensure_tenant_corpus(tenant_id)}


@app.post("/tasks/run")
async def tasks_run(request: Request) -> dict[str, Any]:
    """Scheduler tick: execute any due tasks/reminders/followups."""
    if config.TASKS_TOKEN and request.headers.get("x-tasks-token") != config.TASKS_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

    ran = 0
    skipped = 0
    status_cache: dict[str, str] = {}
    for task in clients.due_tasks():
        tid = task.get("tenant_id") or config.DEFAULT_TENANT
        # Honour each tenant's run-state; paused/stopped tenants' tasks stay pending.
        st = status_cache.get(tid)
        if st is None:
            st = clients.get_agent_status(tid)
            status_cache[tid] = st
        if st != "running":
            skipped += 1
            continue
        instruction = (
            f"Scheduled {task.get('type', 'task')} is due. Carry it out now: "
            f"{task.get('description', '')}"
        )
        try:
            session_id = clients.ensure_session(tid, state=_session_state(tid))
            clients.query_agent(user_id=tid, session_id=session_id, message=_framed(tid, instruction))
            clients.mark_task(task["id"], "done")
            ran += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("task %s (tenant %s) failed", task.get("id"), tid)
            clients.record_alert(
                "task_failed",
                f"scheduled task {task.get('id')} failed: {exc}",
                tenant_id=tid,
                severity="error",
            )
            clients.mark_task(task["id"], "error")
    return {"ran": ran, "skipped": skipped}
