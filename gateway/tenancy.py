"""Tenant registry + identity resolution (multi-tenant routing).

Phase 1 — data model only, no behavior change. The gateway uses ``resolve_tenant``
to map an inbound sender (email address / phone number) to the tenant whose agent
the message should reach.

The pure normalization helpers (``normalize_email`` / ``normalize_phone`` /
``identity_key``) are dependency-free so they can be mirrored verbatim into the
agent package in Phase 3 without dragging in the gateway's Firestore layer.
"""
from __future__ import annotations

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
