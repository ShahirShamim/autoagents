"""Tenant registry + identity resolution (multi-tenant routing).

Phase 1 — data model only, no behavior change. The gateway uses ``resolve_tenant``
to map an inbound sender (email address / phone number) to the tenant whose agent
the message should reach.

The pure normalization helpers (``normalize_email`` / ``normalize_phone`` /
``identity_key``) are dependency-free so they can be mirrored verbatim into the
agent package in Phase 3 without dragging in the gateway's Firestore layer.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from gateway import clients, config


# --------------------------------------------------------------------------- #
# Identity normalization (pure, no I/O)
# --------------------------------------------------------------------------- #
def normalize_email(addr: str) -> str:
    """Lowercase + strip an email address, unwrapping a ``Name <addr>`` form."""
    a = (addr or "").strip()
    if "<" in a and ">" in a:
        a = a[a.rfind("<") + 1 : a.rfind(">")]
    return a.strip().lower()


def normalize_phone(num: str) -> str:
    """Reduce a phone number to bare digits (drops +, spaces, punctuation)."""
    return re.sub(r"\D", "", num or "")


def identity_key(channel: str, value: str) -> str:
    """Stable identities doc id: ``email:<addr>`` / ``phone:<digits>``."""
    if channel == "email":
        return f"email:{normalize_email(value)}"
    if channel == "whatsapp":
        return f"phone:{normalize_phone(value)}"
    raise ValueError(f"unknown channel {channel!r}")


# --------------------------------------------------------------------------- #
# Registry (Firestore)
# --------------------------------------------------------------------------- #
def resolve_tenant(channel: str, value: str) -> str | None:
    """Map an inbound sender to its ``tenant_id`` via the identities collection.

    Returns the tenant_id, or ``None`` if the sender is not registered — the
    caller decides whether to reject or treat it as pending/onboarding.
    """
    key = identity_key(channel, value)
    snap = clients.db().collection(config.COL_IDENTITIES).document(key).get()
    if not snap.exists:
        return None
    return (snap.to_dict() or {}).get("tenant_id")


def tenant_config(tenant_id: str) -> dict[str, Any] | None:
    """Fetch a tenant document (status, emails, phones, notes), or ``None``."""
    snap = clients.db().collection(config.COL_TENANTS).document(tenant_id).get()
    return (snap.to_dict() | {"id": snap.id}) if snap.exists else None


def create_tenant(
    tenant_id: str,
    name: str,
    *,
    status: str = "active",
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Create or merge a tenant document. Idempotent; preserves ``created_at``."""
    ref = clients.db().collection(config.COL_TENANTS).document(tenant_id)
    doc: dict[str, Any] = {
        "name": name,
        "status": status,
        "emails": [normalize_email(e) for e in (emails or [])],
        "phones": [normalize_phone(p) for p in (phones or [])],
        "notes": notes,
    }
    if not ref.get().exists:
        doc["created_at"] = clients.now_iso()
    ref.set(doc, merge=True)
    return doc | {"id": tenant_id}


def set_tenant_status(tenant_id: str, status: str) -> None:
    """Update a tenant's lifecycle status ('pending'/'active'/'disabled')."""
    clients.db().collection(config.COL_TENANTS).document(tenant_id).set(
        {"status": status, "status_at": clients.now_iso()}, merge=True
    )


def activate_tenant(tenant_id: str) -> None:
    """Flip a pending tenant to active (used on first-message onboarding)."""
    set_tenant_status(tenant_id, "active")


def add_identity(tenant_id: str, channel: str, value: str) -> str:
    """Register an identity → tenant mapping. Returns the identity key. Idempotent."""
    key = identity_key(channel, value)
    clients.db().collection(config.COL_IDENTITIES).document(key).set(
        {
            "tenant_id": tenant_id,
            "channel": channel,
            "value": value,
            "linked_at": clients.now_iso(),
        },
        merge=True,
    )
    return key


def agent_context(tenant_id: str) -> str:
    """Operator-authored standing instructions for this tenant's agent (free text),
    edited in the admin panel and prepended to every agent turn. Empty if unset."""
    return ((tenant_config(tenant_id) or {}).get("agent_context") or "").strip()


def primary_email(tenant_id: str) -> str:
    """The tenant owner's primary email (where third-party replies are relayed)."""
    emails = (tenant_config(tenant_id) or {}).get("emails", []) or []
    return emails[0] if emails else ""


def primary_whatsapp(tenant_id: str) -> str:
    """A WhatsApp address to reach the tenant owner: their first registered phone,
    else a linked whatsapp identity value (e.g. a LID)."""
    cfg = tenant_config(tenant_id) or {}
    phones = cfg.get("phones") or []
    if phones:
        return normalize_phone(phones[0])
    for d in (
        clients.db().collection(config.COL_IDENTITIES).where("tenant_id", "==", tenant_id).stream()
    ):
        v = d.to_dict()
        if v.get("channel") == "whatsapp" and v.get("value"):
            return str(v["value"])
    return ""


# --------------------------------------------------------------------------- #
# Third-party reply threads (Phase 4)
# --------------------------------------------------------------------------- #
def parse_tagged_tenant(addr: str) -> str | None:
    """Extract the tenant id from a reply-routable address.

    The agent emails third parties from ``assistant+<tenant_id>@jmkn.tech``; a
    reply lands on that address, so the ``+<tenant_id>`` tag tells us which tenant
    initiated the thread. Returns the tenant id only if it is a real tenant.
    """
    local, _, _domain = normalize_email(addr).partition("@")
    if "+" not in local:
        return None
    _base, _, tag = local.partition("+")
    if tag and tenant_config(tag):
        return tag
    return None


def thread_doc_id(tenant_id: str, channel: str, contact: str) -> str:
    """Deterministic thread id so re-sends + replies hit the same row."""
    c = normalize_email(contact) if channel == "email" else normalize_phone(contact)
    return f"{tenant_id}:{channel}:{c}"


def apply_thread_ttl(
    tenant_id: str, channel: str, contact: str, latest_outbound_at: str
) -> tuple[str, bool]:
    """Enforce the third-party access window. Returns ``(disposition, courtesy)``:

    - ``("process", False)`` — within the window (or first reply): feed to the agent.
    - ``("blocked", True)``  — just expired: drop, and send ONE courtesy note.
    - ``("blocked", False)`` — already expired + already notified: drop silently.

    The clock starts at the contact's FIRST reply (``first_reply_at``); a fresh
    outbound to the same contact *after* that (``latest_outbound_at`` newer than
    the first reply) reopens a new window.
    """
    ref = clients.db().collection(config.COL_THREADS).document(
        thread_doc_id(tenant_id, channel, contact)
    )
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}
    now = dt.datetime.now(dt.UTC)
    nowi = now.isoformat()
    first = data.get("first_reply_at")

    # Reopen: the agent re-sent to this contact after their first reply.
    if first and latest_outbound_at and latest_outbound_at > first:
        first = None

    base = {
        "tenant_id": tenant_id, "channel": channel, "contact": contact,
        "last_at": nowi, "created_at": data.get("created_at", nowi),
    }

    if not first:
        # First reply (or reopened): start a fresh 3h window.
        expires = (now + dt.timedelta(hours=config.THREAD_TTL_HOURS)).isoformat()
        ref.set({**base, "first_reply_at": nowi, "expires_at": expires,
                 "status": "active", "closed_notified": False}, merge=True)
        return "process", False

    if nowi <= data.get("expires_at", ""):
        # Still inside the window.
        ref.set({**base, "status": "active"}, merge=True)
        return "process", False

    # Expired.
    if not data.get("closed_notified"):
        ref.set({**base, "status": "expired", "closed_notified": True}, merge=True)
        return "blocked", True
    ref.set({**base, "status": "expired"}, merge=True)
    return "blocked", False
