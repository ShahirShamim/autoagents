# autoagents — Improvement Plan

Implementation plan for every fix identified in the July 2026 codebase review.
Each fix is self-contained: problem, exact code changes, and verification steps.
Written so any coding model can implement a fix without re-analysing the repo.

## How to use this document

- **Work one fix at a time.** Each fix = one commit (or one small PR). Use the
  fix ID (e.g. `F1`) in the commit message.
- **Line numbers are approximate** — they were correct when this plan was
  written, but earlier fixes shift them. Always locate code by the quoted
  snippet, never by line number alone.
- **Do not change anything not named in the fix.** In particular, never change
  the model name `gemini-3.5-flash` (see `autoagents-agent/CLAUDE.md`).
- Python style: match the existing code (ruff, line-length 88, type hints,
  double quotes). JS style: match `whatsapp-bridge/index.js` (ESM, 2-space).
- After every Python change: from the repo root run
  `python -c "import gateway.main"` or `python -c "import admin.main"`
  (with the service's requirements installed) to catch syntax/import errors.
- Fixes marked **[operator]** need a human with GCP access — the model should
  prepare the code/commands but not execute deploys or secret rotation.

## Fix index

| ID | Title | Priority | Area | Effort |
|----|-------|----------|------|--------|
| F1 | Auth must fail closed when secrets are unset | Critical | gateway, admin | S |
| F2 | Remove cross-tenant inbound-email fallback | Critical | gateway | S |
| F3 | Rotate exposed secrets **[operator]** | Critical | ops | S |
| F4 | Admin panel hardening (CSRF, open redirect, rate limit, replay, timing) | High | admin | M |
| F5 | Unit tests for routing/ownership logic | High | gateway | M |
| F6 | Transactional session pointer (`ensure_session`) | High | gateway | M |
| F7 | Retries + dead-letter for message delivery | High | gateway, bridge | M |
| F8 | Fix bridge creds-backup race | High | bridge | S |
| F9 | Firestore indexes + bounded queries | Medium | gateway, admin, agent | M |
| F10 | Cache the Vertex Agent Engine handle | Medium | gateway | S |
| F11 | Delete dead code | Medium | all | S |
| F12 | Bridge lockfile + reproducible install | Medium | bridge | S |
| F13 | GitHub Actions CI | Medium | repo | S |
| F14 | Container hardening (non-root, .dockerignore) | Medium | all Dockerfiles | S |
| F15 | Fix packaging bugs in pyproject.toml | Medium | agent | S |
| F16 | Input hardening (attachment size cap, jid validation, PII logs, RAG guard) | Medium | gateway, bridge, agent | S |
| F17 | Documentation sweep (stale/contradictory docs) | Low | docs | M |
| F18 | Root README + LICENSE + repo hygiene | Low | repo | S |
| F19 | Remove PII defaults; register `send_whatsapp` over MCP | Low | gateway, admin, agent | S |

---

## F1 — Auth must fail closed when secrets are unset (Critical)

**Files:** `gateway/main.py`, `admin/main.py`

**Problem.** Several auth checks silently disable themselves when their secret
env var is empty. A deployment with a missing secret becomes an open service:

1. `gateway/main.py` `_verify_webhook`: returns `True` when
   `RESEND_WEBHOOK_SECRET` is unset → anyone can POST forged inbound email.
2. `gateway/main.py` `/internal/wa-link/{tenant_id}`, `/internal/ensure-corpus/{tenant_id}`,
   `/tasks/run`: all use the pattern
   `if config.TASKS_TOKEN and request.headers.get("x-tasks-token") != config.TASKS_TOKEN`
   — an empty `TASKS_TOKEN` skips the check entirely.
3. `gateway/main.py` `_link_serializer`: signs with the literal string
   `"unset"` when `LINK_SECRET` is empty.
4. `admin/main.py` `_signer`: falls back to `"unset"` as the signing key when
   both `MAGIC_SECRET` and `ADMIN_PASSWORD` are empty — anyone can forge a
   valid admin session cookie offline.

### Changes

**1. `gateway/main.py` — `_verify_webhook`.** Replace:

```python
    if not config.RESEND_WEBHOOK_SECRET:
        log.warning("RESEND_WEBHOOK_SECRET unset — skipping signature verification")
        return True
```

with:

```python
    if not config.RESEND_WEBHOOK_SECRET:
        log.error("RESEND_WEBHOOK_SECRET unset — rejecting webhook (fail closed)")
        return False
```

**2. `gateway/main.py` — token-gated endpoints.** Add a helper near
`_verify_webhook`:

```python
def _require_tasks_token(request: Request) -> None:
    """Gate internal endpoints on the shared X-Tasks-Token. Fails CLOSED: a
    missing TASKS_TOKEN config means nobody is authorised, not everybody."""
    if not config.TASKS_TOKEN or request.headers.get("x-tasks-token") != config.TASKS_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
```

Then in all three handlers (`internal_wa_link`, `internal_ensure_corpus`,
`tasks_run`) replace the two-line pattern:

```python
    if config.TASKS_TOKEN and request.headers.get("x-tasks-token") != config.TASKS_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
```

with:

```python
    _require_tasks_token(request)
```

**3. `gateway/main.py` — link tokens.** Replace:

```python
def _link_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.LINK_SECRET or "unset", salt="aa-walink")


def _make_link_token(tenant_id: str) -> str:
    return _link_serializer().dumps(tenant_id)
```

with:

```python
def _link_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.LINK_SECRET, salt="aa-walink")


def _make_link_token(tenant_id: str) -> str:
    if not config.LINK_SECRET:
        raise HTTPException(status_code=503, detail="LINK_SECRET not configured")
    return _link_serializer().dumps(tenant_id)
```

(`_load_link_token` already checks `config.LINK_SECRET` before calling the
serializer, so it needs no change.)

**4. `admin/main.py` — session/magic-link signing.** Replace:

```python
def _signer(salt: str) -> URLSafeTimedSerializer:
    key = config.MAGIC_SECRET or config.ADMIN_PASSWORD or "unset"
    return URLSafeTimedSerializer(key, salt=salt)
```

with:

```python
def _signer(salt: str) -> URLSafeTimedSerializer:
    key = config.MAGIC_SECRET or config.ADMIN_PASSWORD
    if not key:
        # Fail closed: with no signing key configured, no session or magic
        # link can ever be valid — and none can be minted.
        raise RuntimeError("MAGIC_SECRET (or ADMIN_PASSWORD) must be configured")
    return URLSafeTimedSerializer(key, salt=salt)
```

And make `_authed` treat the unconfigured state as "not signed in" instead of
crashing every page:

```python
def _authed(request: Request) -> bool:
    tok = request.cookies.get(config.COOKIE_NAME)
    if not tok:
        return False
    try:
        _serializer().loads(tok, max_age=config.SESSION_MAX_AGE)
        return True
    except RuntimeError:
        return False  # no signing key configured → deny
    except (BadSignature, SignatureExpired):
        return False
```

`/login` (POST), `/auth`, and `/login/password` will raise a 500 when the key
is missing — that is acceptable fail-closed behaviour (nobody can sign in).

### Verify

- `python -c "import gateway.main, admin.main"` succeeds.
- With env vars **unset**, run `uvicorn gateway.main:app --port 8081` and:
  - `curl -X POST localhost:8081/inbound/email -d '{}'` → **401**.
  - `curl -X POST localhost:8081/tasks/run` → **401**.
  - `curl -X POST localhost:8081/internal/wa-link/tenant_0` → **401**.
- With env vars unset, run `uvicorn admin.main:app --port 8082` and:
  - `curl -i localhost:8082/` → 303 redirect to `/login` (not 500).
  - A cookie forged with key `"unset"` no longer authenticates:
    `python -c "from itsdangerous import URLSafeTimedSerializer as S; print(S('unset', salt='aa-admin').dumps('ok'))"`,
    then `curl -i localhost:8082/ -H "Cookie: aa_admin=<that value>"` → still
    redirects to `/login`.
- After F5 exists, add these as pytest cases.

---

## F2 — Remove cross-tenant inbound-email fallback (Critical)

**File:** `gateway/main.py`, route `inbound_email`

**Problem.** When a Resend `email.received` webhook id isn't retrievable, the
handler falls back to `clients.latest_inbound_id()` — the globally most recent
inbound email, **any tenant's**. Under concurrent inbound traffic this can
fetch a different tenant's email and process it as this sender's message
(cross-tenant data leak + wrong-recipient replies).

Current code (after `email_id = data.get("email_id") or data.get("id") or ""`):

```python
    full = clients.fetch_inbound_email(email_id) if email_id else {}
    if "received" in etype and not full.get("id"):
        # Webhook id wasn't a retrievable inbound id — fall back to most recent.
        email_id = clients.latest_inbound_id()
        full = clients.fetch_inbound_email(email_id) if email_id else {}
```

### Change

The fallback exists because the retrieve path is unverified against live
Resend payloads, so don't delete it outright — **constrain it**: only accept
the fallback email if its `from` matches the webhook's own `from` metadata.
Replace the block above with:

```python
    full = clients.fetch_inbound_email(email_id) if email_id else {}
    if "received" in etype and not full.get("id"):
        # Webhook id wasn't retrievable. The old behaviour fell back to the
        # globally most-recent inbound email, which under concurrency could be
        # a DIFFERENT tenant's mail. Only accept the fallback when its sender
        # matches this webhook's own metadata; otherwise process metadata-only.
        fb_id = clients.latest_inbound_id()
        fb = clients.fetch_inbound_email(fb_id) if fb_id else {}
        if fb.get("id") and _as_email(fb.get("from")) == _as_email(data.get("from")):
            email_id, full = fb_id, fb
        else:
            log.error("inbound email %s not retrievable; processing metadata only", email_id)
            clients.record_alert(
                "inbound_email_unretrievable",
                f"webhook id {email_id!r} not retrievable and fallback sender mismatched",
                severity="warning",
            )
```

Everything after (`src = full or data`) stays unchanged — when the fallback is
rejected, the handler proceeds with the webhook's own metadata (`data`), which
is always the correct sender, just possibly without a body.

### Verify

- Import check passes.
- Unit test (goes in F5's test file): monkeypatch
  `clients.fetch_inbound_email` to return `{}` for the webhook id and a full
  email with a *different* `from` for the fallback id; assert the handler logs
  the alert and the message logged to Firestore uses the webhook's `from`, not
  the fallback's. (Test via calling the route function with a fake `Request`,
  or factor the fallback logic into a small pure helper and test that.)

---

## F3 — Rotate exposed secrets (Critical) **[operator]**

**Problem.** `steps.md` and `docs/HUMAN_GUIDE.md` record that the admin
break-glass password, `RESEND_API_KEY`, and `WHATSAPP_BRIDGE_SECRET` were
exposed in chat logs during the beta build. No rotation is recorded anywhere.
`autoagents-agent/.env` also holds live values of `RESEND_API_KEY` and
`WHATSAPP_BRIDGE_SECRET` (gitignored, but live).

### Steps (human with `gcloud` access to project `autoagents-500500`)

1. Generate new values:
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` for
   `WHATSAPP_BRIDGE_SECRET`, `TASKS_TOKEN`, `LINK_SECRET`, `MAGIC_SECRET`, and
   the admin password. Create a new API key in the Resend dashboard (and
   revoke the old one there).
2. Add new secret versions (names may differ — confirm with
   `gcloud secrets list`):
   ```bash
   printf '%s' "<new value>" | gcloud secrets versions add resend-api-key --data-file=-
   printf '%s' "<new value>" | gcloud secrets versions add whatsapp-bridge-secret --data-file=-
   printf '%s' "<new value>" | gcloud secrets versions add admin-password --data-file=-
   printf '%s' "<new value>" | gcloud secrets versions add admin-magic-secret --data-file=-
   ```
3. Redeploy every consumer so it picks up the new versions: the gateway and
   admin Cloud Run services, the whatsapp-bridge container on the e2-micro VM,
   and the agent (`./deploy.sh` in `autoagents-agent/` — its `secretEnv` is
   sticky, so a redeploy refreshes values).
   The bridge's `WA_SECRET` and the gateway's `WHATSAPP_BRIDGE_SECRET` must be
   updated **together** or WhatsApp send/receive breaks.
4. Update `autoagents-agent/.env` locally with the new values.
5. Disable the old secret versions:
   `gcloud secrets versions disable <n> --secret=<name>`.
6. Record the rotation date in `steps.md` and remove the "needs rotating"
   notes from `docs/HUMAN_GUIDE.md` and `docs/MULTI_TENANT_PLAN.md`.

### Verify

- Send a test email and WhatsApp message end to end.
- Old Resend key rejected: `curl https://api.resend.com/emails -H "Authorization: Bearer <old key>"` → 401.

---

## F4 — Admin panel hardening (High)

**Files:** `admin/main.py`, `admin/config.py`

Five sub-fixes. Implement together — they touch the same handlers.

### F4a — CSRF tokens on all state-changing POSTs

**Problem.** Every mutating route (`/tenants`, `/t/{tid}/identity`,
`/t/{tid}/runstate`, `/t/{tid}/lifecycle`, `/t/{tid}/agent-context`,
`/t/{tid}/wa-link`, `/alerts/{aid}/resolve`) is a cookie-authenticated form
POST with no CSRF token; only `SameSite=Lax` protects them.

**Design.** Put a random nonce in the (already signed) session cookie; every
form carries it as a hidden field; every POST handler compares them.

1. At the top of `admin/main.py` add imports:

   ```python
   import secrets as pysecrets
   ```

2. Change the session payload from the constant `"ok"` to a dict with a nonce.
   Replace `_set_session` with:

   ```python
   def _set_session(resp: Response) -> Response:
       payload = {"v": "ok", "csrf": pysecrets.token_urlsafe(16)}
       resp.set_cookie(
           config.COOKIE_NAME,
           _serializer().dumps(payload),
           max_age=config.SESSION_MAX_AGE,
           httponly=True,
           secure=config.COOKIE_SECURE,
           samesite="lax",
       )
       return resp
   ```

3. Add a session reader and a combined guard (place next to `_authed`, which
   becomes a thin wrapper):

   ```python
   def _session(request: Request) -> dict | None:
       tok = request.cookies.get(config.COOKIE_NAME)
       if not tok:
           return None
       try:
           data = _serializer().loads(tok, max_age=config.SESSION_MAX_AGE)
       except RuntimeError:
           return None  # no signing key configured → deny (see F1)
       except (BadSignature, SignatureExpired):
           return None
       return data if isinstance(data, dict) and data.get("v") == "ok" else None


   def _authed(request: Request) -> bool:
       return _session(request) is not None


   def _guard(request: Request, csrf: str | None = None) -> Response | None:
       """None if the request may proceed; otherwise the response to return.
       Pass the submitted csrf form value on every state-changing POST."""
       sess = _session(request)
       if not sess:
           return _redirect("/login")
       if csrf is not None and not pysecrets.compare_digest(
           csrf or "", sess.get("csrf", "")
       ):
           return Response(status_code=403, content="csrf check failed")
       return None
   ```

   Note: existing sessions become invalid (old cookies decode to the string
   `"ok"`, not a dict) — the operator just signs in again. That is fine.

4. In **every** handler that currently starts with
   `if not _authed(request): return _redirect("/login")`:
   - GET handlers (`index`, `tenant_detail`): replace with
     `if (r := _guard(request)) is not None: return r`.
   - POST handlers: add a form param `csrf: str = Form("")` to the signature
     and replace the auth check with
     `if (r := _guard(request, csrf)) is not None: return r`.
   The POST handlers to update: `create_tenant`, `edit_identity`,
   `edit_runstate`, `edit_lifecycle`, `edit_agent_context`, `send_wa_link`,
   `resolve_alert_route`. (`/login` and `/login/password` are pre-auth — leave
   them without CSRF.)

5. Thread the token into the HTML. In `index` and `tenant_detail`, after the
   guard passes, get it once:

   ```python
   csrf = (_session(request) or {}).get("csrf", "")
   ```

   Then add `<input type=hidden name=csrf value='{esc(csrf)}'>` inside **every**
   `<form method=post ...>` those pages render. Grep for `method=post` in
   `admin/main.py` to find them all; don't miss the per-row forms built inside
   `render_alerts` (pass `csrf` as a new parameter: `render_alerts(alerts, back, csrf)`)
   and the `id_rows`, `state_btn`, `life_btn` closures inside `tenant_detail`
   (they close over local variables, so `csrf` is available).

### F4b — Open redirect in alert resolve

In `resolve_alert_route`, replace:

```python
    return _redirect(back if back.startswith("/") else "/")
```

with:

```python
    return _redirect(back if back.startswith("/") and not back.startswith("//") else "/")
```

### F4c — Rate-limit magic-link sends

Add near the auth helpers (module level):

```python
import time

_LOGIN_SENDS: list[float] = []  # timestamps of recent magic-link sends
_LOGIN_WINDOW_S = 600
_LOGIN_MAX_SENDS = 3


def _login_throttled() -> bool:
    now = time.time()
    while _LOGIN_SENDS and now - _LOGIN_SENDS[0] > _LOGIN_WINDOW_S:
        _LOGIN_SENDS.pop(0)
    if len(_LOGIN_SENDS) >= _LOGIN_MAX_SENDS:
        return True
    _LOGIN_SENDS.append(now)
    return False
```

In `login_magic`, wrap the send:

```python
    if tenancy.normalize_email(email) == config.ADMIN_EMAIL and not _login_throttled():
        _send_magic_email(config.ADMIN_EMAIL, _magic_link(config.ADMIN_EMAIL))
    return _redirect("/login?sent=1")
```

(Always redirect to `sent=1` — the response must stay identical whether
throttled or not, so the allowlist remains unprobeable. In-memory state is
fine: the admin service runs a single instance.)

### F4d — Make magic links single-use

Add to `admin/tenancy.py`:

```python
def mark_token_used(token_hash: str) -> bool:
    """Record a redeemed magic-link token. True if it was fresh, False if replayed."""
    from google.api_core import exceptions as gexc

    ref = db().collection("admin_used_tokens").document(token_hash)
    try:
        ref.create({"ts": now_iso()})
        return True
    except gexc.AlreadyExists:
        return False
```

In `admin/main.py` `auth()`, after the email check passes and before
`_set_session`:

```python
    import hashlib

    if not tenancy.mark_token_used(hashlib.sha256(token.encode()).hexdigest()):
        return _redirect("/login?bad=1")
```

(Docs in the collection are tiny and only one is written per sign-in; no
cleanup needed. `create()` is atomic, so concurrent redeems can't both win.)

### F4e — Constant-time password compare + secure cookie default

In `login_password`, replace:

```python
    if config.ADMIN_PASSWORD and password == config.ADMIN_PASSWORD:
```

with:

```python
    if config.ADMIN_PASSWORD and pysecrets.compare_digest(password, config.ADMIN_PASSWORD):
```

In `admin/config.py`, flip the `COOKIE_SECURE` default (the app is served on
public HTTPS at `admin.autoagents.jmkn.tech`; localhost-proxy users can set
`COOKIE_SECURE=false` explicitly):

```python
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
```

Update the comment above it to say the default is now secure and the env var
exists for the localhost-proxy case.

### Verify

- `python -c "import admin.main"`.
- Run locally with `MAGIC_SECRET=test COOKIE_SECURE=false uvicorn admin.main:app --port 8082`
  (Firestore calls need creds — for a UI smoke test use
  `gcloud auth application-default login`, or just verify the auth paths):
  - Sign in via password (set `ADMIN_PASSWORD=test`), open a tenant page,
    view source: every `<form method=post>` contains a `csrf` hidden input.
  - Submit a state-changing form with the csrf value edited → 403.
  - `curl -X POST 'localhost:8082/alerts/x/resolve' -d 'back=//evil.com&csrf=...' -H 'Cookie: ...'`
    → redirect `Location: /` (not `//evil.com`).
  - POST `/login` 4× within 10 min with the admin email → Resend called at
    most 3× (watch the logs).
  - Open the same magic link twice → second attempt lands on `/login?bad=1`.

---

## F5 — Unit tests for routing/ownership logic (High)

**Files:** new `gateway/tests/__init__.py` (empty), new
`gateway/tests/test_tenancy.py`, new `gateway/tests/test_routing.py`;
`gateway/requirements-dev.txt` (new)

**Problem.** The functions that enforce the tenant-isolation boundary have
zero tests: `tenancy.normalize_email/normalize_phone/identity_key`,
`tenancy.parse_tagged_tenant`, `main._route_sender`, `main._wa_is_owner`,
`main._as_email`, `main._to_addresses`, `main._model_supported`.

### Steps

1. Create `gateway/requirements-dev.txt`:

   ```
   -r requirements.txt
   pytest>=8
   ```

2. Create `gateway/tests/__init__.py` (empty file).

3. Create `gateway/tests/test_tenancy.py` — pure functions, no mocking:

   ```python
   """Tests for the pure identity-normalization helpers."""
   import pytest

   from gateway import tenancy


   def test_normalize_email_plain():
       assert tenancy.normalize_email("  Foo@Example.COM ") == "foo@example.com"


   def test_normalize_email_display_name():
       assert tenancy.normalize_email("Jane Doe <Jane@Ex.com>") == "jane@ex.com"


   def test_normalize_email_empty():
       assert tenancy.normalize_email("") == ""
       assert tenancy.normalize_email(None) == ""


   def test_normalize_phone():
       assert tenancy.normalize_phone("+92 307-025 1725") == "923070251725"
       assert tenancy.normalize_phone("") == ""


   def test_identity_key():
       assert tenancy.identity_key("email", "A@B.c") == "email:a@b.c"
       assert tenancy.identity_key("whatsapp", "+1 555") == "phone:1555"
       with pytest.raises(ValueError):
           tenancy.identity_key("carrier-pigeon", "x")
   ```

4. Create `gateway/tests/test_routing.py` — monkeypatch the Firestore-touching
   functions so no credentials are needed:

   ```python
   """Tests for inbound routing and owner-vs-third-party resolution."""
   from gateway import main, tenancy


   # --- _route_sender -------------------------------------------------------
   def test_route_unknown_sender_rejected(monkeypatch):
       monkeypatch.setattr(tenancy, "resolve_tenant", lambda c, v: None)
       assert main._route_sender("email", "nobody@x.com") == (None, "reject")


   def test_route_pending_tenant_onboards(monkeypatch):
       monkeypatch.setattr(tenancy, "resolve_tenant", lambda c, v: "t1")
       monkeypatch.setattr(tenancy, "tenant_config", lambda t: {"status": "pending"})
       assert main._route_sender("email", "a@x.com") == ("t1", "onboard")


   def test_route_disabled_tenant_rejected(monkeypatch):
       monkeypatch.setattr(tenancy, "resolve_tenant", lambda c, v: "t1")
       monkeypatch.setattr(tenancy, "tenant_config", lambda t: {"status": "disabled"})
       assert main._route_sender("email", "a@x.com") == ("t1", "reject")


   def test_route_active_tenant(monkeypatch):
       monkeypatch.setattr(tenancy, "resolve_tenant", lambda c, v: "t1")
       monkeypatch.setattr(tenancy, "tenant_config", lambda t: {"status": "active"})
       assert main._route_sender("email", "a@x.com") == ("t1", "active")


   # --- _wa_is_owner --------------------------------------------------------
   def test_wa_owner_matches_registered_phone(monkeypatch):
       monkeypatch.setattr(
           tenancy, "resolve_tenant",
           lambda c, v: "t1" if (c, v) == ("whatsapp", "111") else None,
       )
       assert main._wa_is_owner("t1", "111", "", "999@s.whatsapp.net") is True


   def test_wa_third_party_is_not_owner(monkeypatch):
       monkeypatch.setattr(tenancy, "resolve_tenant", lambda c, v: None)
       assert main._wa_is_owner("t1", "222", "333", "222") is False


   def test_wa_other_tenants_number_is_not_owner(monkeypatch):
       # The sender IS a registered identity — but of a different tenant.
       monkeypatch.setattr(tenancy, "resolve_tenant", lambda c, v: "t2")
       assert main._wa_is_owner("t1", "111", "", "111") is False


   # --- tagged third-party reply addresses ----------------------------------
   def test_parse_tagged_tenant(monkeypatch):
       monkeypatch.setattr(
           tenancy, "tenant_config", lambda t: {"id": t} if t == "t7" else None
       )
       assert tenancy.parse_tagged_tenant("assistant+t7@jmkn.tech") == "t7"
       assert tenancy.parse_tagged_tenant("assistant+ghost@jmkn.tech") is None
       assert tenancy.parse_tagged_tenant("assistant@jmkn.tech") is None


   # --- payload helpers ------------------------------------------------------
   def test_as_email_variants():
       assert main._as_email("A@B.com") == "a@b.com"
       assert main._as_email(["X@Y.com", "z@w.com"]) == "x@y.com"
       assert main._as_email([{"email": "Q@R.com"}]) == "q@r.com"
       assert main._as_email(None) == ""


   def test_to_addresses_mixed_shapes():
       got = main._to_addresses(
           {"to": "A@b.com"},
           {"to": [{"email": "C@d.com"}, "E@f.com"]},
           {"nope": 1},
       )
       assert got == ["a@b.com", "c@d.com", "e@f.com"]


   def test_model_supported():
       assert main._model_supported("application/pdf")
       assert main._model_supported("image/png")
       assert not main._model_supported("application/zip")
   ```

   Important: `main._route_sender` calls `tenancy.resolve_tenant` via the
   module attribute, so patching `gateway.tenancy` attributes (as above) works.
   Do **not** patch `gateway.main.tenancy` differently — it is the same module
   object.

5. Run from the **repo root** (so `gateway` is importable as a package):

   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -r gateway/requirements-dev.txt
   python -m pytest gateway/tests -q
   ```

### Verify

All tests pass with **no** GCP credentials configured (they must not touch
Firestore — if any test errors with credential problems, a patch is missing).

---

## F6 — Transactional session pointer in `ensure_session` (High)

**File:** `gateway/clients.py`

**Problem.** `ensure_session` does an unguarded read-modify-write of the
`agent_sessions/<user_id>` pointer doc. Two concurrent inbound messages for
one tenant can both decide to create a session; one pointer write clobbers the
other, orphaning a session (and any memory flush tied to it).

**Design.** The engine calls (create/flush) stay **outside** the transaction —
Firestore transactions retry their function, so no network side effects
inside. Only the final pointer write is transactional: it re-checks that the
pointer hasn't changed since we read it; if a concurrent writer won, we adopt
their session id instead of overwriting.

### Change

In `ensure_session`, replace the last block:

```python
    created = engine.create_session(user_id=user_id, state=state or {})
    new_sid = created.get("id") if isinstance(created, dict) else created.id
    pref.set({"session_id": new_sid, "last_at": nowi, "tenant_id": user_id})
    return new_sid
```

with:

```python
    created = engine.create_session(user_id=user_id, state=state or {})
    new_sid = created.get("id") if isinstance(created, dict) else created.id
    return _claim_session_pointer(pref, prev_sid=sid, new_sid=new_sid,
                                  nowi=nowi, user_id=user_id)
```

And add this helper above `ensure_session`:

```python
def _claim_session_pointer(
    pref: Any, *, prev_sid: str | None, new_sid: str, nowi: str, user_id: str
) -> str:
    """Atomically publish a freshly created session id to the pointer doc.

    If another request rotated the pointer concurrently (it no longer holds
    ``prev_sid``), adopt the winner's session instead of clobbering it — the
    session we created is simply left unused.
    """
    transaction = db().transaction()

    @firestore.transactional
    def _txn(txn: Any) -> str:
        snap = pref.get(transaction=txn)
        cur = (snap.to_dict() or {}).get("session_id")
        if cur and cur != prev_sid and cur != new_sid:
            txn.set(pref, {"last_at": nowi}, merge=True)
            return cur
        txn.set(pref, {"session_id": new_sid, "last_at": nowi, "tenant_id": user_id})
        return new_sid

    try:
        return _txn(transaction)
    except Exception:  # noqa: BLE001 - degraded mode: behave like the old code
        log.exception("session pointer transaction failed; writing directly")
        pref.set({"session_id": new_sid, "last_at": nowi, "tenant_id": user_id})
        return new_sid
```

Also apply the same claim to the "adopt existing session" branch — replace:

```python
        adopted = _latest_matching_session(engine, user_id, want)
        if adopted:
            pref.set(
                {"session_id": adopted, "last_at": nowi, "tenant_id": user_id}, merge=True
            )
            return adopted
```

with:

```python
        adopted = _latest_matching_session(engine, user_id, want)
        if adopted:
            return _claim_session_pointer(pref, prev_sid=None, adopted_ok=True,
                                          new_sid=adopted, nowi=nowi, user_id=user_id)
```

…and to keep the helper simple, `prev_sid=None` already covers this case
(pointer had no session id, so `cur and cur != prev_sid` triggers only when a
concurrent writer set one). Drop the `adopted_ok` idea — call it exactly as:

```python
            return _claim_session_pointer(pref, prev_sid=None, new_sid=adopted,
                                          nowi=nowi, user_id=user_id)
```

### Verify

- Import check passes.
- Unit test with a fake pointer/transaction is overkill; instead do a live
  concurrency probe after deploy (operator): fire two simultaneous WhatsApp
  messages at one tenant after >8h idle and confirm `agent_sessions/<tid>`
  ends up with exactly one `session_id` and both replies arrive.

---

## F7 — Retries + dead-letter for message delivery (High)

**Files:** `gateway/clients.py`, `gateway/main.py`, `whatsapp-bridge/index.js`

**Problem.**
1. Bridge → gateway inbound forwarding (`postInbound`) is a single `fetch`; a
   gateway 5xx or timeout silently **drops the user's message**.
2. `send_email` / `send_whatsapp` in the gateway are single attempts.
3. A failed scheduled task is marked `"error"` permanently — never retried.

### F7a — gateway outbound retry helper

In `gateway/clients.py`, add near the top (after `now_iso`):

```python
import time


def _post_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: int = 30,
    attempts: int = 3,
) -> requests.Response:
    """POST with retries on network errors and 5xx (never on 4xx — those are
    deterministic and retrying would double-send). Raises the last network
    error if every attempt fails."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
            if resp.status_code < 500:
                return resp
            last_exc = None
            last_resp = resp
        except requests.RequestException as exc:
            last_exc = exc
        if i < attempts - 1:
            time.sleep(2**i)  # 1s, 2s
    if last_exc is not None:
        raise last_exc
    return last_resp
```

In `send_email`, replace the `requests.post("https://api.resend.com/emails", ...)`
call with:

```python
        resp = _post_with_retry(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json_body={
                "from": config.SENDER_EMAIL,
                "to": [to],
                "subject": subject,
                "text": body,
            },
            timeout=30,
        )
```

In `send_whatsapp`, replace the `requests.post(...bridge.../send, ...)` call
the same way (keep the surrounding try/except and logging exactly as they are).

Additionally, in **both** functions, when the final result is not ok, record
an alert so failures surface in the admin panel instead of only in logs. At
the end of `send_email`, before `return`:

```python
    if not ok:
        record_alert("email_send_failed", f"to={to}: {data}", tenant_id=tenant)
```

and at the end of `send_whatsapp`:

```python
    if not ok:
        record_alert("whatsapp_send_failed", f"to={to}: {data}", tenant_id=tenant)
```

### F7b — bridge inbound retry + GCS dead-letter

In `whatsapp-bridge/index.js`, replace `postInbound` with:

```js
async function postInbound(payload) {
  if (!GATEWAY_INBOUND_URL) return;
  const delays = [0, 2000, 8000]; // 3 attempts
  for (let i = 0; i < delays.length; i++) {
    if (delays[i]) await new Promise((r) => setTimeout(r, delays[i]));
    try {
      const r = await fetch(GATEWAY_INBOUND_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-WA-Secret": WA_SECRET },
        body: JSON.stringify(payload),
      });
      if (r.ok) return;
      // 4xx is deterministic — don't retry, but do dead-letter it.
      console.error("gateway inbound", r.status, (await r.text()).slice(0, 200));
      if (r.status < 500) break;
    } catch (e) {
      console.error("gateway inbound error", e.message);
    }
  }
  // All attempts failed: park the payload in GCS so the message isn't lost.
  try {
    const dest = `wa-deadletter/${new Date().toISOString().slice(0, 10)}/${Date.now()}.json`;
    await bucket.file(dest).save(JSON.stringify(payload), {
      contentType: "application/json",
      resumable: false,
    });
    console.error(`inbound dead-lettered to gs://${BUCKET}/${dest}`);
  } catch (e) {
    console.error("dead-letter write failed", e.message);
  }
}
```

### F7c — scheduled-task retry budget

In `gateway/clients.py`, add:

```python
def bump_task_attempts(task_id: str, attempts: int) -> None:
    """Record a failed run; the task stays pending so the next tick retries it."""
    db().collection(config.COL_TASKS).document(task_id).update(
        {"attempts": attempts, "last_error_at": now_iso()}
    )
```

In `gateway/main.py` `tasks_run`, replace the failure branch:

```python
            clients.mark_task(task["id"], "error")
```

with:

```python
            attempts = int(task.get("attempts", 0)) + 1
            if attempts >= 3:
                clients.mark_task(task["id"], "error")
            else:
                clients.bump_task_attempts(task["id"], attempts)
```

(The task keeps `status == "pending"`, so `due_tasks()` picks it up again on
the next scheduler tick — retries are spaced by the tick interval, no tight
loop. The existing `record_alert("task_failed", ...)` call above it stays.)

### Verify

- `python -c "import gateway.clients, gateway.main"`; `node --check whatsapp-bridge/index.js`.
- Retry helper test (add to `gateway/tests/`): monkeypatch `requests.post` to
  raise `requests.ConnectionError` twice then succeed → `_post_with_retry`
  returns the response; make it return a 400 → exactly one call happens.
- Bridge: run locally with `GATEWAY_INBOUND_URL=http://localhost:9/` (refused
  port), trigger `postInbound({test: 1})` (temporarily via a node REPL import
  or by pointing a test message at it), and check the dead-letter object
  appears in the bucket.

---

## F8 — Fix bridge creds-backup race (High)

**File:** `whatsapp-bridge/index.js`

**Problem.** In `scheduleBackup`, `if (!s || s.backupTimer || s.backingUp) return;`
means a `creds.update` firing **while a backup is running** schedules nothing.
If the process then crashes, the newest creds were never uploaded and the
tenant must re-pair. Also, if `runBackup` throws between setting
`s.backingUp = true` and resetting it, backups stop forever for that tenant
(currently mitigated by the catch, but `fs.readdir` failures leave the flag
logic fragile).

### Change

Replace `scheduleBackup` and `runBackup` with:

```js
function scheduleBackup(tenant) {
  const s = sessions.get(tenant);
  if (!s) return;
  if (s.backingUp) {
    s.pendingBackup = true; // re-run once the in-flight backup finishes
    return;
  }
  if (s.backupTimer) return;
  s.backupTimer = setTimeout(() => runBackup(tenant), 8000);
}

async function runBackup(tenant) {
  const s = sessions.get(tenant);
  if (!s) return;
  s.backupTimer = null;
  s.backingUp = true;
  try {
    const dir = tenantDir(tenant);
    const names = await fs.readdir(dir);
    const obj = {};
    for (const n of names) obj[n] = await fs.readFile(path.join(dir, n), "utf8");
    const gz = gzipSync(Buffer.from(JSON.stringify(obj)));
    await bucket.file(blobPath(tenant)).save(gz, {
      contentType: "application/gzip",
      resumable: false,
    });
  } catch (e) {
    console.error(`[${tenant}] auth backup failed`, e.message);
  } finally {
    s.backingUp = false;
    if (s.pendingBackup) {
      s.pendingBackup = false;
      scheduleBackup(tenant); // catch creds that changed mid-backup
    }
  }
}
```

### Verify

- `node --check whatsapp-bridge/index.js`.
- Logic check: creds.update during backup → `pendingBackup` set → `finally`
  reschedules → second backup runs 8s later. Crash-window shrinks to ≤8s.

---

## F9 — Firestore composite indexes + bounded queries (Medium)

**Files:** `gateway/clients.py`, `admin/main.py`, `admin/tenancy.py`,
`autoagents-agent/app/tools.py`; index creation is **[operator]**

**Problem.** Multiple hot paths stream entire collections and filter in
Python (a deliberate workaround to avoid composite indexes). Read cost and
latency grow linearly with history:

- `gateway/clients.py` `latest_outbound_to` — streams **every** message doc
  for the tenant on every non-owner WhatsApp inbound and every thread reply.
- `admin/tenancy.py` `recent_messages` / `recent_tasks` / `tenant_alerts` /
  `open_alerts` — fetch all matching docs, sort/slice in Python.
- `admin/tenancy.py` `tenant_usage` / `all_usage` — stream the whole `usage`
  collection on every dashboard load.
- `admin/main.py` `index()` — calls `get_run_state(tid)` per tenant (N+1).
- `autoagents-agent/app/tools.py` `query_messages` / `list_tasks` — stream all
  tenant docs then filter/sort in process.

### Step 1 — create the indexes **[operator, do this first]**

```bash
gcloud firestore indexes composite create --collection-group=messages \
  --field-config=field-path=tenant_id,order=ascending \
  --field-config=field-path=ts,order=descending

gcloud firestore indexes composite create --collection-group=messages \
  --field-config=field-path=tenant_id,order=ascending \
  --field-config=field-path=direction,order=ascending \
  --field-config=field-path=channel,order=ascending \
  --field-config=field-path=ts,order=descending

gcloud firestore indexes composite create --collection-group=tasks \
  --field-config=field-path=tenant_id,order=ascending \
  --field-config=field-path=due_at,order=ascending

gcloud firestore indexes composite create --collection-group=alerts \
  --field-config=field-path=resolved,order=ascending \
  --field-config=field-path=ts,order=descending

gcloud firestore indexes composite create --collection-group=alerts \
  --field-config=field-path=tenant_id,order=ascending \
  --field-config=field-path=ts,order=descending
```

Wait until `gcloud firestore indexes composite list` shows them all `READY`
before deploying the code changes below. Also mirror these into
`autoagents-agent/deployment/` if a `firestore.indexes.json` is kept there
(check `autoagents-agent/firestore.indexes.json` — keep the file as the source
of truth and add the new entries).

### Step 2 — `gateway/clients.py` `latest_outbound_to`

Replace the whole loop body with a bounded, indexed query. The `to` field
can't be compared server-side (WhatsApp numbers need digit-normalisation), so
scan only the tenant's most recent 500 outbound messages on that channel:

```python
def latest_outbound_to(tenant_id: str, channel: str, contact: str) -> str:
    """ISO ts of the most recent outbound message this tenant sent to ``contact``.
    (docstring: keep the existing one)"""
    cl = _match_contact(channel, contact)
    if not cl:
        return ""
    q = (
        db()
        .collection(config.COL_MESSAGES)
        .where("tenant_id", "==", tenant_id)
        .where("direction", "==", "out")
        .where("channel", "==", channel)
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(500)
    )
    for d in q.stream():
        r = d.to_dict()
        if _match_contact(channel, r.get("to")) == cl:
            return r.get("ts", "")
    return ""
```

Behaviour note (acceptable): a contact last messaged more than 500 outbound
messages ago is treated as "never messaged" — their reply is dropped as
unsolicited. That is the intended trade-off; mention it in the commit message.

### Step 3 — `admin/tenancy.py` recent/alert queries

Rewrite each to `order_by().limit()` (import stays `from google.cloud import firestore`):

```python
def recent_messages(tid: str, limit: int = 15) -> list[dict[str, Any]]:
    q = (
        db()
        .collection(config.COL_MESSAGES)
        .where("tenant_id", "==", tid)
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [d.to_dict() for d in q.stream()]


def recent_tasks(tid: str, limit: int = 15) -> list[dict[str, Any]]:
    q = (
        db()
        .collection(config.COL_TASKS)
        .where("tenant_id", "==", tid)
        .order_by("due_at")
        .limit(limit)
    )
    return [d.to_dict() | {"id": d.id} for d in q.stream()]


def open_alerts(limit: int = 20) -> list[dict[str, Any]]:
    q = (
        db()
        .collection(config.COL_ALERTS)
        .where("resolved", "==", False)
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(limit)
    )
    return [d.to_dict() | {"id": d.id} for d in q.stream()]


def tenant_alerts(tid: str, limit: int = 10) -> list[dict[str, Any]]:
    q = (
        db()
        .collection(config.COL_ALERTS)
        .where("tenant_id", "==", tid)
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(50)
    )
    docs = [d.to_dict() | {"id": d.id} for d in q.stream() if not d.to_dict().get("resolved")]
    return docs[:limit]
```

### Step 4 — `admin/tenancy.py` usage aggregation

Replace `tenant_usage` with a server-side aggregation (keep the stream loop as
the exception fallback, mirroring the `_count` pattern):

```python
def tenant_usage(tid: str) -> dict[str, int]:
    """Aggregate token usage for a tenant (server-side aggregation, stream fallback)."""
    q = db().collection(config.COL_USAGE).where("tenant_id", "==", tid)
    try:
        aq = q.count(alias="turns")
        aq = aq.sum("prompt_tokens", alias="prompt")
        aq = aq.sum("output_tokens", alias="output")
        aq = aq.sum("thoughts_tokens", alias="thoughts")
        aq = aq.sum("total_tokens", alias="total")
        out = {"turns": 0, "prompt": 0, "output": 0, "thoughts": 0, "total": 0}
        for row in aq.get():
            for r in row:
                out[r.alias] = int(r.value)
        return out
    except Exception:  # noqa: BLE001 - aggregation unsupported → old path
        agg = {"turns": 0, "prompt": 0, "output": 0, "thoughts": 0, "total": 0}
        for d in q.stream():
            r = d.to_dict()
            agg["turns"] += 1
            agg["prompt"] += int(r.get("prompt_tokens", 0))
            agg["output"] += int(r.get("output_tokens", 0))
            agg["thoughts"] += int(r.get("thoughts_tokens", 0))
            agg["total"] += int(r.get("total_tokens", 0))
        return agg
```

Replace `all_usage` to reuse it per tenant (bounded by tenant count, not by
usage history):

```python
def all_usage() -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Per-tenant usage totals + grand total (one aggregation query per tenant)."""
    by: dict[str, dict[str, int]] = {}
    grand = {"turns": 0, "total": 0}
    for t in list_tenants():
        u = tenant_usage(t.get("id", ""))
        by[t["id"]] = {"turns": u["turns"], "total": u["total"]}
        grand["turns"] += u["turns"]
        grand["total"] += u["total"]
    return by, grand
```

(Behaviour note: usage rows whose `tenant_id` doesn't match any tenant doc no
longer appear in the grand total. Fine — every writer sets a real tenant id.)

### Step 5 — `admin` dashboard N+1

Add to `admin/tenancy.py`:

```python
def all_run_states() -> dict[str, str]:
    """tenant_id → run state, one read of the whole agent_state collection."""
    return {
        d.id: (d.to_dict() or {}).get("status", "running")
        for d in db().collection(config.COL_STATE).stream()
    }
```

In `admin/main.py` `index()`, before the loop add
`run_states = tenancy.all_run_states()`, and inside the loop replace
`run_badge(tenancy.get_run_state(tid))` with
`run_badge(run_states.get(tid, "running"))`.

### Step 6 — agent tools

In `autoagents-agent/app/tools.py` (remember: **do not** touch anything else
in this package, per its CLAUDE.md):

`query_messages` — replace the stream-all with:

```python
    q = (
        _client()
        .collection(config.COL_MESSAGES)
        .where("tenant_id", "==", tenant_id)
        .order_by("ts", direction=firestore.Query.DESCENDING)
        .limit(200)
    )
    docs = [d.to_dict() | {"id": d.id} for d in q.stream()]
    if channel:
        docs = [m for m in docs if m.get("channel") == channel]
    return {"messages": docs[: int(limit)]}
```

(Channel stays a client-side filter over the 200 most recent — avoids a third
index. Update the in-code comment accordingly. Drop the now-redundant sort.)

`list_tasks` — replace the stream-all with:

```python
    q = (
        _client()
        .collection(config.COL_TASKS)
        .where("tenant_id", "==", tenant_id)
        .order_by("due_at")
        .limit(200)
    )
    docs = [d.to_dict() | {"id": d.id} for d in q.stream()]
    if status and status != "all":
        docs = [t for t in docs if t.get("status") == status]
    return {"tasks": docs[:100]}
```

### Verify

- Import checks for all three packages.
- **After indexes are READY and services are redeployed** (operator): open the
  admin dashboard and a tenant page — identical data as before; check Cloud
  Run logs for `FailedPrecondition ... requires an index` errors (means an
  index is missing — the error message includes a creation link).
- Send a third-party WhatsApp reply → still relayed (exercises
  `latest_outbound_to`).

---

## F10 — Cache the Vertex Agent Engine handle (Medium)

**File:** `gateway/clients.py`

**Problem.** `query_agent` and `ensure_session` each run `vertexai.init()` +
`agent_engines.get(...)` on **every call** — an extra network round-trip per
message turn.

### Change

Add near the top of `gateway/clients.py` (after the `_gcs` singleton):

```python
_engine: Any = None


def get_engine() -> Any:
    """Cached Agent Engine handle (vertexai.init + get are per-process, once)."""
    global _engine
    if _engine is None:
        import vertexai
        from vertexai import agent_engines

        vertexai.init(project=config.PROJECT_ID, location=config.REGION)
        _engine = agent_engines.get(config.AGENT_ENGINE_RESOURCE)
    return _engine
```

In `query_agent`, replace:

```python
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=config.PROJECT_ID, location=config.REGION)
    engine = agent_engines.get(config.AGENT_ENGINE_RESOURCE)
```

with:

```python
    engine = get_engine()
```

Do the same replacement in `ensure_session`.

### Verify

- Import check; grep confirms no remaining `agent_engines.get(` outside
  `get_engine`.
- After deploy: send two messages; the second turn's latency drops (check
  Cloud Run request latency) and logs show no repeated init.

---

## F11 — Delete dead code (Medium)

**Files:** `autoagents-agent/app/agent.py`, `autoagents-agent/app/retrievers.py`,
`autoagents-agent/pyproject.toml`, `gateway/config.py`, `gateway/clients.py`,
`admin/tenancy.py`

Before each deletion, grep the whole repo for the symbol to confirm it is
unused (`grep -rn "<symbol>" --include="*.py" .`).

1. **`autoagents-agent/app/agent.py`** — the Vertex AI Search tool is built at
   import time but never registered on the agent (it even creates a live
   client object on every import). Delete these lines:

   ```python
   from app.retrievers import create_search_tool
   ```

   ```python
   data_store_region = os.getenv("DATA_STORE_REGION", "global")
   data_store_id = os.getenv("DATA_STORE_ID", "autoagents-agent-collection_documents")
   data_store_path = (
       f"projects/{project_id}/locations/{data_store_region}"
       f"/collections/default_collection/dataStores/{data_store_id}"
   )

   vertex_search_tool = create_search_tool(data_store_path)
   ```

   Then grep for `retrievers`, `create_search_tool`, `vertex_search_tool`,
   `INTEGRATION_TEST` across `autoagents-agent/` — if `tests/` reference them,
   update/remove those references too. Then delete `app/retrievers.py`.

2. **`autoagents-agent/pyproject.toml`** — remove the line
   `"google-cloud-vectorsearch",` from `dependencies` (the spec explicitly
   avoids Vector Search; nothing imports it). Run `uv lock` inside
   `autoagents-agent/` to refresh the lockfile.

3. **`gateway/config.py`** — delete the `ADMIN_WHATSAPP = [...]` block and the
   `ADMIN_EMAILS = [...]` block (including their comments). Both are relics of
   the single-tenant design; nothing in `gateway/` references them (verify by
   grep before deleting).

4. **`gateway/clients.py`** — delete `fetch_inbound_attachment` (never called)
   and `_debug_dump` (never called). After removing `_debug_dump`, the
   `import json` at the top becomes unused — remove it.

5. **`admin/tenancy.py`** — delete `open_alert_count` (never referenced by
   `admin/main.py`).

### Verify

- `python -c "import gateway.main, gateway.clients, admin.main, admin.tenancy"`.
- In `autoagents-agent/`: `uv run python -c "import app.agent"` and
  `uv run pytest tests/unit`.
- `grep -rn "ADMIN_EMAILS\|ADMIN_WHATSAPP\|vertex_search_tool\|fetch_inbound_attachment\|_debug_dump\|open_alert_count" --include="*.py" .`
  returns nothing.

---

## F12 — Bridge lockfile + reproducible install (Medium)

**Files:** `whatsapp-bridge/package-lock.json` (new), `whatsapp-bridge/Dockerfile`

**Problem.** No `package-lock.json` is committed, so every image build
resolves dependencies fresh (supply-chain + reproducibility risk). Baileys is
pinned to a release candidate (`7.0.0-rc13`).

### Steps

1. In `whatsapp-bridge/`, run `npm install` (Node ≥ 20). Commit the generated
   `package-lock.json`. Make sure `.gitignore` does not exclude it
   (root `.gitignore` has node entries — check for a `package-lock.json` line
   and remove it if present).
2. Check whether a stable Baileys 7.x exists: `npm view baileys versions --json | tail -20`.
   - If a stable `7.0.0` (no `-rc`) or later 7.x exists: bump
     `package.json` to that exact version (keep it exact, no `^`), run
     `npm install` again, and re-commit the lockfile. **Flag this for manual
     QR-pairing re-test before production** — Baileys minor versions break
     pairing regularly.
   - If not: keep `7.0.0-rc13` (it is at least pinned exactly) and note in the
     commit message that no stable release exists yet.
3. Update `whatsapp-bridge/Dockerfile` to use the lockfile:

   ```dockerfile
   COPY package.json package-lock.json ./
   RUN npm ci --omit=dev
   ```

   (replacing the current `COPY package.json ./` + `RUN npm install --omit=dev`).

### Verify

- `docker build whatsapp-bridge/` succeeds locally (or on the VM).
- `npm ci --omit=dev` inside the directory exits 0.

---

## F13 — GitHub Actions CI (Medium)

**File:** new `.github/workflows/ci.yml`

**Problem.** No CI at all — nothing runs lint or tests before a deploy.

### Steps

Create `.github/workflows/ci.yml`:

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  gateway:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r gateway/requirements-dev.txt ruff
      - run: ruff check gateway
      - run: python -m pytest gateway/tests -q

  admin:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r admin/requirements.txt ruff
      - run: ruff check admin
      - run: python -c "import admin.main"

  bridge:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
      - run: npm ci --omit=dev
        working-directory: whatsapp-bridge
      - run: node --check index.js
        working-directory: whatsapp-bridge

  agent-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff
      - run: ruff check autoagents-agent/app
```

Notes:
- Depends on F5 (`gateway/requirements-dev.txt`, tests) and F12 (bridge
  lockfile for `npm ci`). If implementing CI first, temporarily use
  `pip install -r gateway/requirements.txt ruff` + drop the pytest step, and
  `npm install` instead of `npm ci`.
- The agent job lints only: its unit tests import modules that call
  `google.auth.default()` at import time, which fails without credentials.
  Do not add GCP credentials to CI in this pass.
- If `ruff check` fails on pre-existing style issues, fix trivial ones
  (unused imports etc.); for anything non-trivial, add a per-file
  `# noqa` or narrow the ruff invocation and note it in the commit.

### Verify

Push a branch; all four jobs green in the Actions tab.

---

## F14 — Container hardening (Medium)

**Files:** `gateway/Dockerfile`, `admin/Dockerfile`,
`whatsapp-bridge/Dockerfile`, new `admin/.dockerignore`

**Problem.** All three containers run as root. `admin/` has no
`.dockerignore`, so local `__pycache__`/`.env` can leak into images.

### Steps

1. **`gateway/Dockerfile`** and **`admin/Dockerfile`** — after the `COPY . …`
   line and before `CMD`, add:

   ```dockerfile
   RUN useradd --create-home --uid 1000 app
   USER app
   ```

2. **`whatsapp-bridge/Dockerfile`** — the bridge writes creds to
   `AUTH_DIR` (default `/data/auth`), so that path must be writable by the
   non-root user. The `node` image ships a `node` user (uid 1000). After
   `COPY index.js ./` add:

   ```dockerfile
   RUN mkdir -p /data/auth && chown -R node:node /data
   USER node
   ```

3. Create `admin/.dockerignore` (mirror `gateway/.dockerignore` if it exists,
   else use):

   ```
   __pycache__/
   *.pyc
   .env
   .venv/
   venv/
   ```

### Verify

- `docker build` each service.
- `docker run --rm <image> id` → uid 1000, not root.
- Bridge container starts and can `mkdir` under `/data/auth` (watch startup
  logs for permission errors).

---

## F15 — Fix packaging bugs in pyproject.toml (Medium)

**File:** `autoagents-agent/pyproject.toml`

**Problem.**
1. `[tool.hatch.build.targets.wheel] packages = ["app","frontend"]` — there is
   no `frontend/` directory; a wheel build fails.
2. `[tool.ty.environment] python-version = "3.10"` contradicts
   `requires-python = ">=3.11,<3.14"` and ruff's `target-version = "py311"`.
3. `[tool.ruff.lint.isort] known-first-party = ["app", "frontend"]` — same
   phantom package.

### Changes

```toml
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

```toml
[tool.ty.environment]
python-version = "3.11"
```

```toml
[tool.ruff.lint.isort]
known-first-party = ["app"]
```

### Verify

In `autoagents-agent/`: `uv build` produces a wheel; `uv run ruff check app`
still passes.

---

## F16 — Input hardening (Medium)

**Files:** `gateway/main.py`, `gateway/config.py`, `whatsapp-bridge/index.js`,
`autoagents-agent/app/tools.py`

### F16a — cap attachment downloads (gateway)

**Problem.** `_store_attachments` does `requests.get(url)` on an
attacker-influenced `download_url` with no size limit — memory blowup on a
small instance.

In `gateway/config.py` add:

```python
# Refuse to buffer inbound attachments larger than this (webhook-supplied URLs).
MAX_ATTACHMENT_MB = int(os.environ.get("MAX_ATTACHMENT_MB", "25"))
```

In `gateway/main.py` `_store_attachments`, replace:

```python
        if url:
            try:
                r = requests.get(url, timeout=60)
                if r.ok:
                    content = r.content
            except Exception:  # noqa: BLE001
                content = None
```

with:

```python
        if url:
            content = _bounded_download(url)
```

and add the helper above `_store_attachments`:

```python
def _bounded_download(url: str) -> bytes | None:
    """Download at most MAX_ATTACHMENT_MB; None on failure or oversize."""
    cap = config.MAX_ATTACHMENT_MB * 1024 * 1024
    try:
        with requests.get(url, timeout=60, stream=True) as r:
            if not r.ok:
                return None
            declared = int(r.headers.get("content-length") or 0)
            if declared > cap:
                log.warning("attachment too large (%s bytes declared); skipping", declared)
                return None
            buf = bytearray()
            for chunk in r.iter_content(1 << 20):
                buf.extend(chunk)
                if len(buf) > cap:
                    log.warning("attachment exceeded %sMB cap; skipping", config.MAX_ATTACHMENT_MB)
                    return None
            return bytes(buf)
    except Exception:  # noqa: BLE001
        return None
```

### F16b — validate `/send` jid (bridge)

**Problem.** `/send` accepts any raw string containing `@` as a jid — a group
(`@g.us`) or broadcast id could be targeted.

In `whatsapp-bridge/index.js` `/send`, replace:

```js
  const jid = String(to).includes("@")
    ? String(to)
    : String(to).replace(/\D/g, "") + "@s.whatsapp.net";
```

with:

```js
  const raw = String(to);
  let jid;
  if (raw.includes("@")) {
    // Only 1:1 destinations — never groups (@g.us), broadcasts, or newsletters.
    if (!raw.endsWith("@s.whatsapp.net") && !raw.endsWith("@lid")) {
      return res.status(400).json({ error: "only 1:1 recipients allowed" });
    }
    jid = raw;
  } else {
    const num = raw.replace(/\D/g, "");
    if (!num) return res.status(400).json({ error: "invalid recipient" });
    jid = num + "@s.whatsapp.net";
  }
```

### F16c — stop logging message text + full numbers (bridge)

In the `messages.upsert` handler, replace the `console.log` that includes
`text.slice(0, 50)`:

```js
      console.log(
        `[${tenant}] inbound from ${from.slice(0, 6)}… (pn:${pn ? "y" : "-"} lid:${lid ? "y" : "-"})` +
          `${text ? ` text:${text.length}ch` : ""}${media ? " [media]" : ""}`,
      );
```

(Keeps enough to debug routing — tenant, number prefix, payload shape — drops
message content and full numbers from stdout logs.)

### F16d — guard `search_documents` (agent)

**Problem.** Unlike every other tool, `search_documents` does not wrap its I/O
— a RAG/permission error hard-fails the tool call instead of returning
`{"ok": False}` for the model to handle.

In `autoagents-agent/app/tools.py` `search_documents`, wrap the query and
parsing in try/except (keep everything else identical):

```python
    from vertexai import rag

    _ensure_rag()
    try:
        resp = rag.retrieval_query(
            text=query,
            rag_resources=[rag.RagResource(rag_corpus=corpus)],
            rag_retrieval_config=rag.RagRetrievalConfig(top_k=int(top_k)),
        )
    except Exception as exc:  # noqa: BLE001 - degrade like the other tools
        return {"ok": False, "error": str(exc), "contexts": []}
```

### Verify

- Import checks + `node --check`.
- F16a: unit-testable — monkeypatch `requests.get` to return a fake streaming
  response with a huge `content-length` → returns `None`.
- F16b: `curl -X POST bridge/send -d '{"tenant":"t","to":"123@g.us","text":"x"}' ...` → 400.

---

## F17 — Documentation sweep (Low)

**Files:** `README.md` (root of `autoagents-agent/`), `docs/*.md`, `steps.md`,
`.agents-cli-spec.md`

**Problem.** Several docs contradict the shipped code. Fix each named item;
change nothing else in these files.

1. **`docs/MULTI_TENANT_SCOPE.md`** — "Decision A: SHARED corpus +
   tenant_id metadata filter" is presented as locked, but the implementation
   uses **per-tenant corpora** (see `gateway/clients.py ensure_tenant_corpus`
   and `app/tools.py _corpus`). Do not rewrite history: add a clearly marked
   banner directly under the Decision A heading:

   > **SUPERSEDED (Phase 2):** implementation moved to a per-tenant RAG corpus
   > (physical isolation) — see MULTI_TENANT_PLAN.md and AGENT_GUIDE.md §…
   > This section is kept for the historical record.

2. **`docs/AGENT_GUIDE.md`** — the tools list still includes
   `PreloadMemoryTool()` + `after_agent_callback=generate_memories_callback`,
   which were removed in Phase 3 (memory is gateway-orchestrated now; see
   `gateway/clients.py _memory_facts/_store_memory`). Update that section to
   describe the current wiring. Also update the tool list to the real 12 tools
   in `app/agent.py` (adds `web_search`, `send_whatsapp`).

3. **`docs/HUMAN_GUIDE.md`** — the "parameters/operations" section still
   documents the retired single shared WhatsApp number (`+44 7340 926493`) and
   the old `/qr?token=` flow as current. Mark that section SUPERSEDED (same
   banner style as AGENT_GUIDE §3.10) and point to the per-tenant self-service
   linking flow (`/link?token=`, gateway `internal_wa_link`). Also update the
   memory-wiring mention (same as item 2), and reconcile the two
   contradictory statements about gemini-3.5-flash pricing (state rates as
   unknown/estimated in ONE place; delete the other).

4. **`steps.md`** — the documentation index (~line 122) lists only 3 docs and
   says multi-tenant is "not yet built". Update: list all five docs
   (`AGENT_GUIDE`, `HUMAN_GUIDE`, `MULTI_TENANT_SCOPE`, `MULTI_TENANT_PLAN`,
   plus this `improvements.md`) and state multi-tenant shipped (Phases 0–7,
   per MULTI_TENANT_PLAN.md).

5. **`autoagents-agent/README.md`** — currently stale scaffold boilerplate
   ("Document Q&A with RAG pipeline") pointing at a nonexistent `GEMINI.md`.
   Rewrite briefly: what the agent is (multi-tenant personal assistant brain
   on Vertex AI Agent Runtime), its 12 tools, how it relates to the gateway,
   how to run tests/deploy (`deploy.sh`), pointing to `CLAUDE.md` and
   `docs/AGENT_GUIDE.md`.

6. **`.agents-cli-spec.md`** — historical origin spec; do not rewrite. Add one
   line at the top: "Historical origin spec (June 2026). Current architecture:
   see docs/AGENT_GUIDE.md."

### Verify

Grep checks: `grep -rn "PreloadMemoryTool" docs/` returns only
SUPERSEDED-marked text; `grep -rn "GEMINI.md" autoagents-agent/` returns
nothing; `grep -n "not yet built" steps.md` returns nothing.

---

## F18 — Root README + LICENSE + repo hygiene (Low)

**Files:** new `README.md` (repo root), new `LICENSE`, `prompt.txt`, `.gitignore`

1. **Root `README.md`** — nothing ties the five components together. Write a
   short one (~60 lines):
   - What autoagents is (one paragraph).
   - Component table: `autoagents-agent` (ADK brain on Agent Runtime),
     `gateway` (FastAPI webhook/event layer, Cloud Run), `admin` (operator
     panel, Cloud Run), `whatsapp-bridge` (Baileys, e2-micro VM), `docs`.
   - ASCII data-flow diagram: user → Resend/WhatsApp → gateway → Agent
     Runtime → Resend/bridge → user; Firestore + GCS shared.
   - Pointers: `docs/HUMAN_GUIDE.md` (operations), `docs/AGENT_GUIDE.md`
     (architecture), `improvements.md` (this plan).
   - How to run tests (`python -m pytest gateway/tests`).
2. **`LICENSE`** — every source file carries an Apache-2.0 header but there is
   no license file. Add the standard Apache License 2.0 text as `LICENSE` at
   the repo root.
3. **`prompt.txt`** — the original raw brief, committed at root. Move it to
   `docs/history/prompt.txt` (`git mv`).
4. **`.DS_Store`** — already gitignored; delete the stray file on disk
   (`find . -name .DS_Store -delete`). No commit needed if untracked.

### Verify

`git status` clean after commit; README renders correctly on the repo page.

---

## F19 — Remove PII defaults + MCP tool drift (Low)

**Files:** `admin/config.py`, `gateway/scripts/migrate_phase1.py`,
`autoagents-agent/app/config.py`, `autoagents-agent/app/mcp_server.py`

1. **`admin/config.py`** — the operator's personal Gmail is the baked-in
   default trust anchor:

   ```python
   ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "shahirshamim15314@gmail.com").strip().lower()
   ```

   Change to an empty default (fail closed — with no `ADMIN_EMAIL` configured,
   no magic link can ever be requested, because no submitted email equals ""
   after the `normalize_email(email) == config.ADMIN_EMAIL` check… **but**
   `normalize_email("") == ""` would match an empty submission, so also guard
   the login route):

   ```python
   ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
   ```

   And in `admin/main.py` `login_magic`, change the condition to:

   ```python
       if (
           config.ADMIN_EMAIL
           and tenancy.normalize_email(email) == config.ADMIN_EMAIL
           and not _login_throttled()
       ):
   ```

   **[operator]**: set `ADMIN_EMAIL` explicitly on the Cloud Run service
   before deploying this change, or sign-in breaks.

2. **`gateway/scripts/migrate_phase1.py`** — a one-off migration that already
   ran, with personal emails + a phone number hardcoded. Delete the file
   (it is preserved in git history if ever needed).

3. **`autoagents-agent/app/config.py`** — `ADMIN_EMAILS` defaults to two real
   personal addresses. Grep `autoagents-agent/` for `ADMIN_EMAILS`: if nothing
   outside `config.py` uses it, delete the block; if something does, change
   the default to `""` (empty list) and require the env var.

4. **`autoagents-agent/app/mcp_server.py`** — the registration loop omits
   `tools.send_whatsapp`, so the "same tools over MCP" claim is stale. Add
   `tools.send_whatsapp,` to the tuple (after `tools.send_email,`).

### Verify

- Import checks; `grep -rn "shahirshamim\|jmkntech\|923070251725" --include="*.py" .`
  returns nothing.
- MCP: `uv run python -c "from app import mcp_server; print(sorted(t for t in mcp_server.mcp._tool_manager._tools))"`
  (or simply run the server and list tools) shows 12 tools including
  `send_whatsapp`.

---

## Suggested implementation order

1. **F1, F2** — security fail-closed (small, highest value). Deploy together.
2. **F3** — operator rotates secrets (independent of code).
3. **F5** — tests (protects everything after).
4. **F4** — admin hardening.
5. **F7, F8, F6** — reliability.
6. **F10, F11, F15, F16** — quick wins.
7. **F9** — indexes first (operator), then query rewrites.
8. **F12, F13, F14** — build/CI/containers (F13 last so it runs green).
9. **F17, F18, F19** — docs + hygiene, any time.

## Global acceptance checklist (after all fixes)

- [ ] All auth paths return 401/403 when their secret is unset (F1).
- [ ] `python -m pytest gateway/tests -q` green with no GCP credentials (F5).
- [ ] CI green on main (F13).
- [ ] `grep -rn "or \"unset\"" gateway admin` returns nothing (F1).
- [ ] No full-collection `.stream()` without `.limit()` in `gateway/clients.py`,
      `admin/tenancy.py`, or `app/tools.py` except `all_run_states`/`list_tenants`
      (bounded by tenant count) (F9).
- [ ] Secrets rotated + rotation date recorded in `steps.md` (F3).
- [ ] End-to-end smoke: email in → reply out; WhatsApp in → reply out;
      third-party reply relayed; scheduled task fires
      (`autoagents-agent/tests/post_deploy.py` covers most of this).
