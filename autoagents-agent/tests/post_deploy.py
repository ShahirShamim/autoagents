"""Post-deployment smoke tests for the LIVE autoagents deployment.

These exercise the real, deployed gateway (Cloud Run), Firestore, and the
per-tenant RAG corpora — end to end. They simulate inbound webhooks with the
same auth the real producers use (WhatsApp bridge secret, Svix-signed Resend
payloads) and assert on the Firestore audit trail + RAG, not on actual message
delivery (which we can't observe).

Coverage:
  1. onboarding                — pending tenant → first email → active + corpus + welcome
  2. whatsapp message → agent  — owner WhatsApp in → agent replies
  3. email message → agent     — owner email in → agent replies
  4. agent whatsapp 3rd party  — a contact replies on the tenant's socket → relayed to owner
  5. agent email 3rd party     — a 3rd party replies to assistant+<tenant>@ → relayed to owner
  6. tenant long-term storage  — per-tenant RAG corpus stores + retrieves a document
  7. no context leak           — tenant A's data/corpus is invisible to tenant B

Run (from autoagents-agent/, with gcloud auth + the project venv):
    uv run pytest tests/post_deploy.py -v -s

Notes:
- Test tenants are created fresh (ids prefixed ``pdt_``) and deleted afterwards.
- Agent-invoking tests cost a few tokens each and take ~10-30s (we poll Firestore).
- Owner addresses use a non-deliverable test subdomain; the agent's reply emails
  to them simply bounce — we only assert they were attempted + logged.
- Secrets are read from Secret Manager via gcloud (cached); override any with an
  env var of the same upper-snake name (e.g. WHATSAPP_BRIDGE_SECRET).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid

import pytest
import requests
from google.cloud import firestore

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "autoagents-500500")
GATEWAY = os.environ.get(
    "GATEWAY_URL", "https://autoagents-gateway-323512451403.us-central1.run.app"
).rstrip("/")
SENDER = os.environ.get("SENDER_EMAIL", "assistant@jmkn.tech")
SENDER_USER, _, SENDER_DOMAIN = SENDER.partition("@")
RAG_LOCATION = os.environ.get("RAG_LOCATION", "us-west1")
ATTACHMENTS_BUCKET = os.environ.get("ATTACHMENTS_BUCKET", f"{PROJECT}-attachments")

DB = firestore.Client(project=PROJECT, database="(default)")

# --------------------------------------------------------------------------- #
# Config / secrets
# --------------------------------------------------------------------------- #
_secret_cache: dict[str, str] = {}


def secret(name: str) -> str:
    """Secret value: env override (UPPER_SNAKE) else Secret Manager via gcloud."""
    env = name.upper().replace("-", "_")
    if os.environ.get(env):
        return os.environ[env]
    if name not in _secret_cache:
        _secret_cache[name] = subprocess.check_output(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={name}", f"--project={PROJECT}"],
            text=True,
        ).strip()
    return _secret_cache[name]


# --------------------------------------------------------------------------- #
# Inbound webhook simulators (same auth the real producers use)
# --------------------------------------------------------------------------- #
def wa_inbound(tenant: str, frm: str, pn: str = "", lid: str = "", text: str = "") -> requests.Response:
    """Simulate the Baileys bridge POSTing an inbound WhatsApp for ``tenant``."""
    return requests.post(
        f"{GATEWAY}/inbound/whatsapp",
        headers={"X-WA-Secret": secret("whatsapp-bridge-secret")},
        json={"tenant": tenant, "from": frm, "pn": pn, "lid": lid, "text": text},
        timeout=60,
    )


def _svix_headers(body: bytes) -> dict[str, str]:
    """Build a valid Svix signature for the Resend webhook secret (whsec_<b64>)."""
    whsec = secret("resend-webhook-secret")
    key = base64.b64decode(whsec.split("_", 1)[1] if "_" in whsec else whsec)
    msg_id = f"msg_{uuid.uuid4().hex}"
    ts = str(int(time.time()))
    signed = f"{msg_id}.{ts}.{body.decode()}".encode()
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}",
            "content-type": "application/json"}


def email_inbound(frm: str, to: str, subject: str, text: str) -> requests.Response:
    """Simulate a Resend inbound-email webhook (no email_id → uses data directly)."""
    body = json.dumps(
        {"type": "email.inbound", "data": {"from": frm, "to": to, "subject": subject, "text": text}}
    ).encode()
    return requests.post(f"{GATEWAY}/inbound/email", headers=_svix_headers(body), data=body, timeout=60)


# --------------------------------------------------------------------------- #
# Firestore helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.UTC).isoformat()


def make_tenant(*, status: str, email: str, phone: str, name: str = "PDT") -> str:
    tid = f"pdt_{uuid.uuid4().hex[:8]}"
    DB.collection("tenants").document(tid).set(
        {"name": name, "status": status, "emails": [email], "phones": [phone],
         "created_at": _now(), "notes": "post-deploy test"}
    )
    DB.collection("identities").document(f"email:{email}").set(
        {"tenant_id": tid, "channel": "email", "value": email, "linked_at": _now()}
    )
    DB.collection("identities").document(f"phone:{phone}").set(
        {"tenant_id": tid, "channel": "whatsapp", "value": phone, "linked_at": _now()}
    )
    return tid


def del_tenant(tid: str, email: str, phone: str) -> None:
    # Best-effort: delete the per-tenant RAG corpus so test runs don't accumulate
    # orphan corpora. Read it off the tenant doc before we delete the doc.
    try:
        corpus = (DB.collection("tenants").document(tid).get().to_dict() or {}).get("rag_corpus")
        if corpus:
            import vertexai
            from vertexai import rag

            vertexai.init(project=PROJECT, location=RAG_LOCATION)
            rag.delete_corpus(name=corpus)
    except Exception:
        pass
    for col, docid in (("identities", f"email:{email}"), ("identities", f"phone:{phone}"),
                       ("tenants", tid), ("agent_sessions", tid), ("agent_state", tid)):
        DB.collection(col).document(docid).delete()
    for col in ("messages", "tasks", "usage"):
        for d in DB.collection(col).where("tenant_id", "==", tid).stream():
            d.reference.delete()


def messages(tid: str, direction: str | None = None, status: str | None = None) -> list[dict]:
    out = []
    for d in DB.collection("messages").where("tenant_id", "==", tid).stream():
        r = d.to_dict()
        if direction and r.get("direction") != direction:
            continue
        if status and r.get("status") != status:
            continue
        out.append(r)
    return out


def log_outbound(tid: str, channel: str, to: str) -> None:
    """Record a prior outbound so a later inbound counts as a genuine reply."""
    DB.collection("messages").document(f"pdt_{uuid.uuid4().hex}").set(
        {"tenant_id": tid, "channel": channel, "direction": "out", "to": to,
         "from": "agent", "body": "earlier outbound", "status": "sent", "ts": _now()}
    )


def wait_for(fn, timeout: float = 75, interval: float = 4):
    """Poll ``fn`` until it returns truthy; return it, else None on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


def fresh_phone() -> str:
    return "99" + uuid.uuid4().int.__str__()[:11]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def active_tenant():
    email = f"pdt-{uuid.uuid4().hex[:8]}@pdt.{SENDER_DOMAIN}"
    phone = fresh_phone()
    tid = make_tenant(status="active", email=email, phone=phone)
    yield {"tid": tid, "email": email, "phone": phone}
    del_tenant(tid, email, phone)


# --------------------------------------------------------------------------- #
# 1. Onboarding: pending tenant → first email → active + corpus + welcome
# --------------------------------------------------------------------------- #
def test_onboarding():
    email = f"pdt-{uuid.uuid4().hex[:8]}@pdt.{SENDER_DOMAIN}"
    phone = fresh_phone()
    tid = make_tenant(status="pending", email=email, phone=phone)
    try:
        r = email_inbound(email, SENDER, "hello", "hi, getting started")
        assert r.status_code == 200, r.text

        def onboarded():
            t = DB.collection("tenants").document(tid).get().to_dict() or {}
            return t.get("status") == "active" and bool(t.get("rag_corpus"))

        assert wait_for(onboarded), "tenant did not activate + get a corpus"
        # a welcome (or agent reply) went out to the owner
        assert wait_for(lambda: messages(tid, direction="out")), "no outbound after onboarding"
    finally:
        del_tenant(tid, email, phone)


# --------------------------------------------------------------------------- #
# 2. WhatsApp message → agent replies
# --------------------------------------------------------------------------- #
def test_whatsapp_to_agent(active_tenant):
    t = active_tenant
    r = wa_inbound(t["tid"], frm=t["phone"], pn=t["phone"], text="what can you do?")
    assert r.status_code == 200, r.text
    assert wait_for(lambda: messages(t["tid"], direction="in", status="received")), "owner WA not received"
    # agent ran and attempted a reply (no linked session for a test tenant → status error, but logged)
    assert wait_for(lambda: [m for m in messages(t["tid"], direction="out") if m.get("body")]), \
        "agent did not produce a WhatsApp reply"


# --------------------------------------------------------------------------- #
# 3. Email message → agent replies
# --------------------------------------------------------------------------- #
def test_email_to_agent(active_tenant):
    t = active_tenant
    r = email_inbound(t["email"], SENDER, "question", "please summarise what you can do")
    assert r.status_code == 200, r.text
    assert wait_for(lambda: messages(t["tid"], direction="in", status="received")), "owner email not received"
    assert wait_for(
        lambda: [m for m in messages(t["tid"], direction="out") if m.get("channel") == "email" and m.get("body")]
    ), "agent did not produce an email reply"


# --------------------------------------------------------------------------- #
# 4. Agent WhatsApp 3rd party: a contact replies on the tenant's socket → relay
# --------------------------------------------------------------------------- #
def test_whatsapp_third_party(active_tenant):
    t = active_tenant
    contact = fresh_phone()  # NOT the owner
    # The agent must have messaged this contact before for the reply to be genuine.
    log_outbound(t["tid"], "whatsapp", contact)
    r = wa_inbound(t["tid"], frm=contact, pn=contact, text="sure, sounds good")
    assert r.status_code == 200, r.text
    assert wait_for(lambda: messages(t["tid"], direction="in", status="thread_reply")), \
        "genuine 3rd-party WhatsApp reply not classified as a thread reply"
    # the agent summarised + relayed to the owner (outbound)
    assert wait_for(lambda: [m for m in messages(t["tid"], direction="out")
                             if m.get("from") == "bridge"]), "no relay outbound to owner"


def test_whatsapp_unsolicited_dropped(active_tenant):
    """A non-owner the agent NEVER messaged must be dropped, not relayed."""
    t = active_tenant
    stranger = fresh_phone()
    r = wa_inbound(t["tid"], frm=stranger, pn=stranger, text="https://spam.example/x")
    assert r.status_code == 200, r.text
    assert wait_for(
        lambda: messages(t["tid"], direction="in", status="rejected_unsolicited"), timeout=30
    ), "unsolicited inbound was not dropped"
    time.sleep(8)  # give any (erroneous) relay time to appear
    relayed = [m for m in messages(t["tid"], direction="out") if m.get("from") == "bridge"]
    assert not relayed, "unsolicited inbound was relayed to the owner!"


# --------------------------------------------------------------------------- #
# 5. Agent email 3rd party: reply to assistant+<tenant>@ → relay to owner
# --------------------------------------------------------------------------- #
def test_email_third_party(active_tenant):
    t = active_tenant
    tagged = f"{SENDER_USER}+{t['tid']}@{SENDER_DOMAIN}"
    third_party = f"vendor-{uuid.uuid4().hex[:6]}@pdt.{SENDER_DOMAIN}"
    r = email_inbound(third_party, tagged, "Re: your note", "yes, let's proceed")
    assert r.status_code == 200, r.text
    assert wait_for(
        lambda: [m for m in messages(t["tid"], status="thread_reply") if m.get("channel") == "email"]
    ), "3rd-party email not routed as a thread reply"
    assert wait_for(
        lambda: [m for m in messages(t["tid"], direction="out") if m.get("channel") == "email"]
    ), "no email relay to owner"


# --------------------------------------------------------------------------- #
# RAG helpers for 6 & 7
# --------------------------------------------------------------------------- #
def _ensure_corpus(tid: str) -> str:
    r = requests.post(
        f"{GATEWAY}/internal/ensure-corpus/{tid}",
        headers={"X-Tasks-Token": secret("tasks-token")},
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("corpus", "")


def _ingest_secret(corpus: str, secret_text: str) -> None:
    """Upload a one-line doc to GCS and import it into the tenant's corpus."""
    from google.cloud import storage

    blob = f"pdt/{uuid.uuid4().hex}.txt"
    storage.Client(project=PROJECT).bucket(ATTACHMENTS_BUCKET).blob(blob).upload_from_string(
        f"The project codeword is {secret_text}.", content_type="text/plain"
    )
    import vertexai
    from vertexai import rag

    vertexai.init(project=PROJECT, location=RAG_LOCATION)
    rag.import_files(corpus_name=corpus, paths=[f"gs://{ATTACHMENTS_BUCKET}/{blob}"])


def _retrieve(corpus: str, query: str) -> str:
    import vertexai
    from vertexai import rag

    vertexai.init(project=PROJECT, location=RAG_LOCATION)
    resp = rag.retrieval_query(
        text=query, rag_resources=[rag.RagResource(rag_corpus=corpus)],
        rag_retrieval_config=rag.RagRetrievalConfig(top_k=5),
    )
    ctxs = getattr(getattr(resp, "contexts", None), "contexts", []) or []
    return " ".join(getattr(c, "text", "") for c in ctxs)


# --------------------------------------------------------------------------- #
# 6. Tenant long-term storage: corpus stores + retrieves a document
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_long_term_storage():
    email = f"pdt-{uuid.uuid4().hex[:8]}@pdt.{SENDER_DOMAIN}"
    phone = fresh_phone()
    tid = make_tenant(status="active", email=email, phone=phone)
    try:
        corpus = _ensure_corpus(tid)
        assert corpus, "no corpus provisioned"
        codeword = f"ZEBRA{uuid.uuid4().hex[:6].upper()}"
        _ingest_secret(corpus, codeword)
        # import is async; poll retrieval until the doc is searchable
        found = wait_for(lambda: codeword in _retrieve(corpus, "what is the project codeword"),
                         timeout=180, interval=10)
        assert found, "ingested document was not retrievable from the corpus"
    finally:
        del_tenant(tid, email, phone)


# --------------------------------------------------------------------------- #
# 7. No context leak between tenants
# --------------------------------------------------------------------------- #
@pytest.mark.slow
def test_no_context_leak():
    a = {"email": f"pdt-{uuid.uuid4().hex[:8]}@pdt.{SENDER_DOMAIN}", "phone": fresh_phone()}
    b = {"email": f"pdt-{uuid.uuid4().hex[:8]}@pdt.{SENDER_DOMAIN}", "phone": fresh_phone()}
    a["tid"] = make_tenant(status="active", email=a["email"], phone=a["phone"], name="PDT-A")
    b["tid"] = make_tenant(status="active", email=b["email"], phone=b["phone"], name="PDT-B")
    try:
        corpus_a = _ensure_corpus(a["tid"])
        corpus_b = _ensure_corpus(b["tid"])
        # (a) physical isolation: distinct corpora by construction
        assert corpus_a and corpus_b and corpus_a != corpus_b, "tenants share a corpus!"

        secret_a = f"FALCON{uuid.uuid4().hex[:6].upper()}"
        _ingest_secret(corpus_a, secret_a)
        assert wait_for(lambda: secret_a in _retrieve(corpus_a, "codeword"), timeout=180, interval=10), \
            "A's own corpus did not return A's secret"

        # (b) B's corpus must never surface A's secret
        assert secret_a not in _retrieve(corpus_b, "codeword"), "LEAK: A's secret retrievable from B"
        assert secret_a not in _retrieve(corpus_b, secret_a), "LEAK: A's secret retrievable from B"

        # (c) Firestore tenancy scoping: a task for A is invisible under B
        DB.collection("tasks").document(f"pdt_{uuid.uuid4().hex}").set(
            {"tenant_id": a["tid"], "description": secret_a, "status": "pending", "due_at": _now()}
        )
        b_tasks = [d.to_dict() for d in DB.collection("tasks").where("tenant_id", "==", b["tid"]).stream()]
        assert all(secret_a not in (t.get("description") or "") for t in b_tasks), "LEAK: A's task seen under B"
    finally:
        del_tenant(a["tid"], a["email"], a["phone"])
        del_tenant(b["tid"], b["email"], b["phone"])
