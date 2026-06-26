"""Firestore operations for the admin webapp.

Mirrors the gateway's tenancy helpers (kept deliberately small + dependency-free
beyond Firestore) since the admin service deploys from its own source root.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from google.cloud import firestore

from admin import config

_db: firestore.Client | None = None


def db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(
            project=config.PROJECT_ID, database=config.FIRESTORE_DATABASE
        )
    return _db


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def normalize_email(addr: str) -> str:
    a = (addr or "").strip()
    if "<" in a and ">" in a:
        a = a[a.rfind("<") + 1 : a.rfind(">")]
    return a.strip().lower()


def normalize_phone(num: str) -> str:
    return re.sub(r"\D", "", num or "")


def identity_key(channel: str, value: str) -> str:
    if channel == "email":
        return f"email:{normalize_email(value)}"
    if channel == "whatsapp":
        return f"phone:{normalize_phone(value)}"
    raise ValueError(f"unknown channel {channel!r}")


# --------------------------------------------------------------------------- #
# Tenants
# --------------------------------------------------------------------------- #
def list_tenants() -> list[dict[str, Any]]:
    out = [d.to_dict() | {"id": d.id} for d in db().collection(config.COL_TENANTS).stream()]
    out.sort(key=lambda t: t.get("id", ""))
    return out


def get_tenant(tid: str) -> dict[str, Any] | None:
    s = db().collection(config.COL_TENANTS).document(tid).get()
    return (s.to_dict() | {"id": s.id}) if s.exists else None


def create_tenant(tid: str, name: str, status: str = "pending", notes: str = "") -> None:
    ref = db().collection(config.COL_TENANTS).document(tid)
    doc: dict[str, Any] = {"name": name, "status": status, "notes": notes}
    if not ref.get().exists:
        doc |= {"emails": [], "phones": [], "created_at": now_iso()}
    ref.set(doc, merge=True)


def set_tenant_status(tid: str, status: str) -> None:
    db().collection(config.COL_TENANTS).document(tid).set(
        {"status": status, "status_at": now_iso()}, merge=True
    )


# --------------------------------------------------------------------------- #
# Identities (routing keys + tenant doc lists kept in sync)
# --------------------------------------------------------------------------- #
def add_identity(tid: str, channel: str, value: str) -> None:
    key = identity_key(channel, value)
    db().collection(config.COL_IDENTITIES).document(key).set(
        {"tenant_id": tid, "channel": channel, "value": value, "linked_at": now_iso()},
        merge=True,
    )
    field = "emails" if channel == "email" else "phones"
    norm = normalize_email(value) if channel == "email" else normalize_phone(value)
    db().collection(config.COL_TENANTS).document(tid).set(
        {field: firestore.ArrayUnion([norm])}, merge=True
    )


def remove_identity(tid: str, channel: str, value: str) -> None:
    key = identity_key(channel, value)
    db().collection(config.COL_IDENTITIES).document(key).delete()
    field = "emails" if channel == "email" else "phones"
    norm = normalize_email(value) if channel == "email" else normalize_phone(value)
    db().collection(config.COL_TENANTS).document(tid).set(
        {field: firestore.ArrayRemove([norm])}, merge=True
    )


# --------------------------------------------------------------------------- #
# Run-state + recent activity
# --------------------------------------------------------------------------- #
def get_run_state(tid: str) -> str:
    s = db().collection(config.COL_STATE).document(tid).get()
    return (s.to_dict() or {}).get("status", "running") if s.exists else "running"


def set_run_state(tid: str, status: str, reason: str = "admin") -> None:
    db().collection(config.COL_STATE).document(tid).set(
        {"status": status, "reason": reason, "updated_at": now_iso()}, merge=True
    )


def recent_messages(tid: str, limit: int = 15) -> list[dict[str, Any]]:
    docs = [
        d.to_dict()
        for d in db().collection(config.COL_MESSAGES).where("tenant_id", "==", tid).stream()
    ]
    docs.sort(key=lambda m: m.get("ts", ""), reverse=True)
    return docs[:limit]


def recent_tasks(tid: str, limit: int = 15) -> list[dict[str, Any]]:
    docs = [
        d.to_dict() | {"id": d.id}
        for d in db().collection(config.COL_TASKS).where("tenant_id", "==", tid).stream()
    ]
    docs.sort(key=lambda t: t.get("due_at", ""))
    return docs[:limit]
