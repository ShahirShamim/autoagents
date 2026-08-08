"""Auth must fail CLOSED, and the inbound-email fallback must not cross tenants.

Covers improvements.md F1 + F2. Run from the repo root:

    pip install -r gateway/requirements.txt -r gateway/requirements-dev.txt
    python -m pytest gateway/tests -q
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gateway import config as gcfg, main as gmain
from admin import config as acfg, main as amain


# ---------------------------------------------------------------- F1.1 webhook
def test_webhook_fails_closed_when_secret_unset(monkeypatch):
    monkeypatch.setattr(gcfg, "RESEND_WEBHOOK_SECRET", "")
    assert gmain._verify_webhook(b"{}", {}) is False, "unsigned webhook accepted"


def test_webhook_still_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(gcfg, "RESEND_WEBHOOK_SECRET", "whsec_" + "a" * 32)
    assert gmain._verify_webhook(b"{}", {"svix-id": "x"}) is False


# ------------------------------------------------------------ F1.2 tasks token
class _Req:
    def __init__(self, tok=None):
        self.headers = {"x-tasks-token": tok} if tok is not None else {}


def test_tasks_token_unset_rejects(monkeypatch):
    monkeypatch.setattr(gcfg, "TASKS_TOKEN", "")
    with pytest.raises(HTTPException) as e:
        gmain._require_tasks_token(_Req("anything"))
    assert e.value.status_code == 503


def test_tasks_token_wrong_rejects(monkeypatch):
    monkeypatch.setattr(gcfg, "TASKS_TOKEN", "secret")
    with pytest.raises(HTTPException) as e:
        gmain._require_tasks_token(_Req("wrong"))
    assert e.value.status_code == 401


def test_tasks_token_missing_header_rejects(monkeypatch):
    monkeypatch.setattr(gcfg, "TASKS_TOKEN", "secret")
    with pytest.raises(HTTPException):
        gmain._require_tasks_token(_Req())


def test_tasks_token_correct_passes(monkeypatch):
    monkeypatch.setattr(gcfg, "TASKS_TOKEN", "secret")
    gmain._require_tasks_token(_Req("secret"))  # must not raise


def test_all_gated_routes_reject_without_token(monkeypatch):
    """End-to-end: every internal/scheduler route is closed with no token set."""
    monkeypatch.setattr(gcfg, "TASKS_TOKEN", "")
    c = TestClient(gmain.app, raise_server_exceptions=False)
    for path in (
        "/tasks/run",
        "/tasks/weekly-keepalive",
        "/internal/wa-link/tenant_0",
        "/internal/ensure-corpus/tenant_0",
    ):
        assert c.post(path).status_code == 503, f"{path} not fail-closed"


# ---------------------------------------------------------- F1.3 link serializer
def test_link_serializer_refuses_without_secret(monkeypatch):
    monkeypatch.setattr(gcfg, "LINK_SECRET", "")
    with pytest.raises(HTTPException):
        gmain._link_serializer()


def test_link_token_roundtrip_with_secret(monkeypatch):
    monkeypatch.setattr(gcfg, "LINK_SECRET", "s3cret")
    assert gmain._load_link_token(gmain._make_link_token("tenant_9")) == "tenant_9"


def test_link_token_not_forgeable_across_secrets(monkeypatch):
    monkeypatch.setattr(gcfg, "LINK_SECRET", "attacker-guess")
    forged = gmain._make_link_token("tenant_0")
    monkeypatch.setattr(gcfg, "LINK_SECRET", "real-secret")
    assert gmain._load_link_token(forged) is None


# --------------------------------------------------------- F1.4 admin signer
def test_admin_signer_refuses_without_any_key(monkeypatch):
    monkeypatch.setattr(acfg, "MAGIC_SECRET", "")
    monkeypatch.setattr(acfg, "ADMIN_PASSWORD", "")
    with pytest.raises(HTTPException):
        amain._signer("aa-admin")


def test_admin_session_cookie_not_forgeable_with_unset_key(monkeypatch):
    """The old code signed with the literal 'unset' — mint that offline and it worked."""
    from itsdangerous import URLSafeTimedSerializer
    forged = URLSafeTimedSerializer("unset", salt="aa-admin").dumps("ok")
    monkeypatch.setattr(acfg, "MAGIC_SECRET", "real-magic-secret")
    monkeypatch.setattr(acfg, "ADMIN_PASSWORD", "pw")

    class R:
        cookies = {acfg.COOKIE_NAME: forged}

    assert amain._authed(R()) is False


# ------------------------------------------------------------- F2 email fallback
def _payload():
    return {"type": "email.received", "data": {"id": "bogus", "from": "victim@x.com"}}


def test_fallback_rejected_when_sender_mismatches(monkeypatch):
    """The attacker's webhook must not pull another tenant's most-recent email."""
    # F1 now rejects unsigned webhooks, so stub verification to reach the F2 path.
    monkeypatch.setattr(gmain, "_verify_webhook", lambda raw, hdrs: True)
    seen = {}

    monkeypatch.setattr(gmain.clients, "fetch_inbound_email",
                        lambda i: {"id": "other", "from": "someone-else@y.com",
                                   "text": "OTHER TENANT SECRET"} if i == "latest" else {})
    monkeypatch.setattr(gmain.clients, "latest_inbound_id", lambda: "latest")
    monkeypatch.setattr(gmain.clients, "record_alert",
                        lambda k, d, **kw: seen.setdefault("alert", k))
    monkeypatch.setattr(gmain, "_store_attachments", lambda i: [])
    monkeypatch.setattr(gmain, "_route_sender", lambda c, s: (None, "reject"))
    monkeypatch.setattr(gmain.clients, "log_message",
                        lambda **kw: seen.setdefault("logged", kw))

    c = TestClient(gmain.app, raise_server_exceptions=False)
    c.post("/inbound/email", json=_payload())

    assert seen.get("alert") == "inbound_email_unretrievable"
    logged = seen.get("logged", {})
    assert "OTHER TENANT SECRET" not in str(logged), "leaked another tenant's body"
    assert logged.get("sender") == "victim@x.com" or "victim@x.com" in str(logged)


def test_fallback_accepted_when_sender_matches(monkeypatch):
    """Legitimate case still works: same sender → body is recovered."""
    # F1 now rejects unsigned webhooks, so stub verification to reach the F2 path.
    monkeypatch.setattr(gmain, "_verify_webhook", lambda raw, hdrs: True)
    seen = {}

    monkeypatch.setattr(gmain.clients, "fetch_inbound_email",
                        lambda i: {"id": "latest", "from": "victim@x.com",
                                   "text": "MY OWN BODY"} if i == "latest" else {})
    monkeypatch.setattr(gmain.clients, "latest_inbound_id", lambda: "latest")
    monkeypatch.setattr(gmain.clients, "record_alert",
                        lambda k, d, **kw: seen.setdefault("alert", k))
    monkeypatch.setattr(gmain, "_store_attachments", lambda i: [])
    monkeypatch.setattr(gmain, "_route_sender", lambda c, s: (None, "reject"))
    monkeypatch.setattr(gmain.clients, "log_message",
                        lambda **kw: seen.setdefault("logged", kw))

    c = TestClient(gmain.app, raise_server_exceptions=False)
    c.post("/inbound/email", json=_payload())

    assert "alert" not in seen, "alerted on a legitimate same-sender fallback"
    assert "MY OWN BODY" in str(seen.get("logged", {}))
