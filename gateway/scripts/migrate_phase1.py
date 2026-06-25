"""Phase 1 migration: stand up the tenant registry + backfill ``tenant_0``.

Idempotent and additive. Does NOT change behavior — existing single-tenant reads
keep working: the ``agent_state`` ``singleton`` doc is preserved (a per-tenant
copy is added alongside it), and existing collections only gain a ``tenant_id``
field. Safe to re-run.

Run from the repo root:

    uv run --with google-cloud-firestore,google-cloud-storage,requests \
        python -m gateway.scripts.migrate_phase1
"""
from __future__ import annotations

from gateway import clients, config, tenancy

TENANT_ID = config.DEFAULT_TENANT  # "tenant_0"
# The original single-tenant owner: their inbound channels route here.
OWNER_EMAILS = ["shahirshamim15314@gmail.com", "jmkntech@gmail.com"]
OWNER_PHONES = ["923070251725"]


def _backfill_collection(name: str) -> int:
    """Add ``tenant_id=tenant_0`` to every doc in ``name`` that lacks it."""
    db = clients.db()
    n = 0
    for d in db.collection(name).stream():
        if "tenant_id" not in (d.to_dict() or {}):
            d.reference.update({"tenant_id": TENANT_ID})
            n += 1
    return n


def main() -> None:
    # 1. tenant_0 registry document
    tenancy.create_tenant(
        TENANT_ID,
        "Owner (migrated)",
        status="active",
        emails=OWNER_EMAILS,
        phones=OWNER_PHONES,
        notes="Original single-tenant user, migrated in Phase 1.",
    )
    print(f"tenant {TENANT_ID} upserted")

    # 2. identities → tenant_0 (point-lookup routing)
    for e in OWNER_EMAILS:
        print("identity:", tenancy.add_identity(TENANT_ID, "email", e))
    for p in OWNER_PHONES:
        print("identity:", tenancy.add_identity(TENANT_ID, "whatsapp", p))

    # 3. backfill tenant_id on existing data (additive)
    for col in (config.COL_MESSAGES, config.COL_TASKS, config.COL_CONTACTS):
        print(f"backfilled {col}:", _backfill_collection(col))

    # 4. per-tenant agent_state: copy the singleton -> tenant_0 (keep singleton)
    db = clients.db()
    snap = db.collection(config.COL_STATE).document(config.STATE_DOC_ID).get()
    state = (
        snap.to_dict() if snap.exists else {"status": "running", "reason": "default"}
    )
    db.collection(config.COL_STATE).document(TENANT_ID).set(state, merge=True)
    print(f"agent_state[{TENANT_ID}] set:", state.get("status"))

    # 5. verify routing resolves
    ok = True
    got_e = tenancy.resolve_tenant("email", OWNER_EMAILS[0])
    ok &= got_e == TENANT_ID
    print(f"VERIFY resolve_tenant(email) -> {got_e}", "OK" if got_e == TENANT_ID else "FAIL")
    got_p = tenancy.resolve_tenant("whatsapp", OWNER_PHONES[0])
    ok &= got_p == TENANT_ID
    print(f"VERIFY resolve_tenant(phone) -> {got_p}", "OK" if got_p == TENANT_ID else "FAIL")
    print("MIGRATION", "OK" if ok else "FAILED")


if __name__ == "__main__":
    main()
