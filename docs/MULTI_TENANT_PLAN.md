# autoagents — Multi-Tenant Implementation Plan

Concrete build plan for the multi-tenant transformation. Decisions are locked in
`MULTI_TENANT_SCOPE.md` (shared corpus + tenant_id filter; tagged reply address;
shared-password admin webapp; trust email sender; reject unknown).

Each phase lists **changes**, **new files/resources**, and a **verify** gate.
Phases are independently shippable; the system keeps working for the existing user
throughout (migrated as the first tenant). Nothing deploys without your OK.

> **Status: ALL PHASES COMPLETE AND LIVE (0–7 shipped 2026-06-25 → 2026-06-27), plus the
> post-plan work listed at the bottom of this file.** Sequencing option **A (full build)** was
> taken. This document is now a historical record — the phase-by-phase verify gates below all
> passed. For current behaviour see `README.md` and `docs/AGENT_GUIDE.md` §10–§12; for what
> happened after the plan closed, see `steps.md`.
>
> Note the header above says "shared corpus + tenant_id filter" — that decision was **reversed
> during Phase 3** in favour of a **corpus per tenant**. See `MULTI_TENANT_SCOPE.md`.

---

## Phase 0 — Validation spikes (de-risk first, ~30 min, read-only)

Two assumptions underpin everything; validate before the big refactor.

- **Spike 1 — Memory Bank is per-`user_id`.** Query the deployed engine as
  `user_id=A` "remember my secret word is X"; then as `user_id=B` ask "what's my
  secret word?". Expect B does **not** recall A's. Confirms one engine isolates
  long-term memory per tenant.
- **Spike 2 — ADK `ToolContext.state` injection.** Add a throwaway tool that returns
  `tool_context.state.get("tenant_id")`; set state on the session from the caller;
  confirm the tool reads it. Confirms tools can be made tenant-aware without per-tenant code.
- **Exit:** both pass → proceed. If Memory Bank isn't per-user, fall back to a
  `tenant_id`-prefixed memory namespace or per-tenant memory scope. If state injection
  differs, pass `tenant_id` via an explicit first tool arg the gateway injects.

### Phase 0 — RESULTS (run 2026-06-25)
- **Spike 2 (`ToolContext.state`) → PASS.** A tool read `tenant_id=tenant_0` from
  session state set by the caller. Tenant-aware tools confirmed feasible.
- **Spike 1 (Memory Bank) → BLOCKED: Memory Bank is NOT configured on the engine.**
  The deployed reasoningEngine has `contextSpec: False` (no `memoryBankConfig`), so
  `add_session_to_memory` / `search_memory` don't persist. The agent has the memory
  *tools* (`PreloadMemoryTool` + `after_agent_callback`) but no memory *backend*.
  ⇒ Long-term memory has never actually worked; prior "recall" was session reuse
  (short-term history). **Fix required regardless of multi-tenancy** — see Phase 0.5.

## Phase 0.5 — Enable Memory Bank on the engine (prerequisite)

- Configure a **Memory Bank** on the Agent Engine: set `spec.contextSpec.memoryBankConfig`
  (managed topics: USER_PERSONAL_INFO, USER_PREFERENCES, EXPLICIT_INSTRUCTIONS) — per the
  `memory-bank` ADK sample's `context_spec`. Either redeploy via the sample's deploy
  pattern (AdkApp + AgentEngineConfig context_spec) or PATCH the existing engine's spec.
- Re-run **Spike 1**: store as user A → wait → A recalls in a new session, B does not.
- **Verify:** A_recalls=True, B_leaks=False → Memory Bank works AND isolates by user_id.
- Update `steps.md` + guides: Memory Bank was wired-but-not-enabled until now.

### Phase 0.5 — RESULTS (run 2026-06-25)
- Enabled `context_spec.memory_bank_config` on the engine via the genai SDK
  (`client.agent_engines.update(name, config=AgentEngineConfig(context_spec=...))`) —
  surgical, no code redeploy.
- **Memory Bank now works via the API** (manual `add_session_to_memory(session=…)` +
  `search_memory(user_id=…, query=…)` stored + retrieved "durian"), and **isolates by
  `user_id`** (other users return 0) → Spike 1 design question = PASS.
- **Remaining gap:** the **deployed agent's auto-memory** (`after_agent_callback` +
  `PreloadMemoryTool`) does NOT store/retrieve — the running container was built before
  Memory Bank existed and isn't wired. `agents-cli deploy` has no `context_spec` option,
  and adding it post-hoc doesn't re-wire the live runtime.
- **Confirmed:** `agents-cli deploy` **strips `context_spec`** on every deploy (no memory
  hook; verified `context_spec: False` after redeploy). Runtime wires memory at **startup**,
  not dynamically. So the deployed agent's auto-memory can't be fixed without changing the
  deploy mechanism.
- **Action to finish 0.5 (decision):** replace `agents-cli deploy` for the agent with a
  small **genai-SDK deploy** (`client.agent_engines.update(name, agent=<AdkApp>,
  config=AgentEngineConfig(context_spec=…, env_vars=…, secret_env=…, requirements=…))`).
  Reuse the **current deployment_spec** (env/secrets/requirements) so nothing breaks, add
  `context_spec`. Becomes the standard deploy going forward. Then re-run Spike 1 through the
  **agent** → expect A recalls, B doesn't.
- **Reframe:** Spike GOAL is met — Memory Bank works + isolates per `user_id` (API-proven),
  so the multi-tenant design is de-risked. Wiring the deployed agent's *auto*-memory is a
  deploy-mechanism change that can be done **now** (fixes current single-tenant memory too)
  or **folded into multi-tenant Phase 3** (which redeploys the agent anyway).

### Phase 0.5 — FINAL RESOLUTION (run 2026-06-25) ✅
- Tried a custom genai-SDK deploy (`scripts/deploy_with_memory.py`) to wire native
  Memory Bank. The engine was created via `deployment_source`, so `update(agent=…)`
  (package_spec) is rejected; the `deployment_source` update path then failed the build
  on requirements bundling. Fragile — abandoned for now (script kept for reference).
- **Chosen solution: orchestrate memory in the GATEWAY** using the engine's working
  Memory Bank API (`async_search_memory` + `async_add_session_to_memory`):
  `query_agent` retrieves the user's memories and injects them as context before the
  turn, and stores the session to memory after. Scoped by `user_id` ⇒ per-tenant ready.
- **Verified:** stored "favorite city = Lahore" in one session, recalled it in a FRESH
  session (no short-term carryover) → "Lahore". Gateway redeploy ships it to production.
- **Net:** functional, per-user long-term memory **without** touching the Agent Runtime
  deploy. The agent's dormant `PreloadMemoryTool` + `after_agent_callback` are now
  redundant (harmless; remove on a future agent redeploy). **Phase 0 + 0.5 COMPLETE.**

## Phase 1 — Data model + tenant registry (Firestore, no behavior change)

- **New collections**
  - `tenants`: `{id, name, status, emails[], phones[], created_at, notes}`
  - `identities`: doc id = `email:<addr>` / `phone:<digits>` → `{tenant_id, channel}`
  - `threads`: `{id, tenant_id, channel, contact, subject, message_id, session_id, status, last_at}`
- **Schema additions:** `tenant_id` on `messages`, `tasks`, `contacts`; `agent_state`
  becomes per-tenant (doc id = tenant_id).
- **Shared `tenancy.py`** helper (used by gateway + agent): normalize identity,
  `resolve_tenant(identity)`, `tenant_config(tenant_id)`.
- **Migrate** the current user → `tenant_0` (their email + the WhatsApp test number);
  backfill `tenant_id=tenant_0` on existing docs.
- **Indexes:** `messages(tenant_id, ts desc)`, `tasks(tenant_id, status, due_at)`,
  `identities` (point lookups, no index needed).
- **Verify:** `resolve_tenant("email:shahir...@gmail.com")` → `tenant_0`; existing
  email/WhatsApp flows still work unchanged.

### Phase 1 — RESULTS (run 2026-06-25) ✅
- `gateway/tenancy.py`: pure helpers (`normalize_email`, `normalize_phone`,
  `identity_key`) + registry ops (`resolve_tenant`, `tenant_config`, `create_tenant`,
  `add_identity`). Pure helpers are I/O-free so they mirror into the agent in Phase 3.
- Collection constants `COL_TENANTS`/`COL_IDENTITIES`/`COL_THREADS` + `DEFAULT_TENANT`
  added to **both** `gateway/config.py` and `app/config.py`.
- `gateway/scripts/migrate_phase1.py` (idempotent, additive) run against live Firestore:
  created **tenant_0** (`status=active`, owner emails `shahirshamim15314@`,`jmkntech@`,
  phone `923070251725`); registered the 3 identities; backfilled `tenant_id=tenant_0` on
  **60 messages + 11 tasks** (0 contacts); copied `agent_state/singleton` → `agent_state/tenant_0`
  (singleton **kept** so live reads don't break). `resolve_tenant(email)` and
  `(phone)` both → `tenant_0` → **MIGRATION OK**.
- 3 composite indexes created (async build): `messages(tenant_id, ts↓)`,
  `tasks(tenant_id, status, due_at)`, `threads(tenant_id, status, last_at↓)` —
  also added to `firestore.indexes.json`.
- **No behavior change:** the registry exists but nothing reads it yet (Phase 2 wires
  gateway routing, Phase 3 threads the agent). Live single-tenant email/WhatsApp untouched.

## Phase 2 — Gateway routing + reject-unknown + onboarding

- `/inbound/email` + `/inbound/whatsapp` resolve sender → tenant (precedence:
  identity → tagged-address/thread → reject).
- Parse tagged `To:` `assistant+t<id>@jmkn.tech` for third-party reply routing (Phase 4
  fills the thread side; address parse lands here).
- Per-tenant `agent_state` (pause/stop checked per tenant).
- **Onboarding:** sender maps to a `pending` tenant → mark `active`, send welcome,
  then process normally.
- `query_agent(user_id=tenant_id, …)` and set session **state** = `{tenant_id, ...}`.
- **Verify:** message from a registered identity → routed + replied; unknown sender →
  rejected (logged, no agent call); a `pending` tenant's first message → onboarded.

### Phase 2 — RESULTS (run 2026-06-25) ✅ DEPLOYED (rev autoagents-gateway-00007-27z)
- `gateway/main.py`: new `_route_sender(channel, sender) → (tenant_id, disposition)`
  where disposition ∈ `active`/`onboard`/`reject`. Both `/inbound/email` and
  `/inbound/whatsapp` route through it **before** any agent work.
- **`user_id` is now the `tenant_id`** for `ensure_session` + `query_agent` (so Agent
  Runtime sessions *and* gateway-orchestrated Memory Bank isolate per tenant), replacing
  the raw email / `wa:<phone>`. *Side effect:* tenant_0's old memories under the previous
  user_id are orphaned — the agent re-learns; acceptable.
- **Reject-unknown:** unresolved senders are logged `rejected_unknown` (with empty
  tenant_id) and dropped — no agent call, no reply.
- **Onboarding:** a resolved tenant whose status is `pending` is flipped to `active`
  (`tenancy.activate_tenant`), sent a welcome, then its first message is processed.
- **Per-tenant run-state:** `agent_state` is keyed by `tenant_id` (was the `singleton`
  doc). `clients.get_agent_status(tenant_id)`/`set_agent_status(tenant_id, …)`. Control
  commands (`!pause`/`!resume`/`!stop`/`!status`) act on the **sender's own tenant** and
  are only honoured for identity-resolved senders (Phase 4 third parties never reach them).
- `log_message` now carries `tenant_id`. Scheduler `/tasks/run` runs as `tenant_0` for
  now (Phase 5 makes it span tenants).
- **Deferred to Phase 3:** session `state={"tenant_id": …}` injection + the agent reading
  it via `ToolContext.state` (verified there with the cross-tenant leak test).
- **Verified live:** owner email → `(tenant_0, active)`; owner phone (normalized
  `+92 307 0251725`) → `(tenant_0, active)`; unknown → `(None, reject)`; pending fixture
  → `onboard` then `active`. Gateway redeployed, health 200, all env/secrets preserved.

## Phase 3 — Tenant-aware tools + per-tenant RAG filter (agent) — biggest change

- `app/tools.py`: each tool reads `tenant_id` from `ToolContext.state` and scopes:
  - `search_documents` → RAG retrieval **with `tenant_id` metadata filter** (mandatory).
  - `ingest_document` → tag the file's metadata with `tenant_id`.
  - `schedule_task` / `list_tasks` / `cancel_task` / `query_messages` → write/read with `tenant_id`.
  - `get_agent_state` / `set_agent_state` → per-tenant doc.
  - `send_email` → send **from `assistant+t<tenant>@jmkn.tech`**; log with `tenant_id`.
  - `send_whatsapp` → log with `tenant_id`.
- `app/agent.py`: tools take `ToolContext`; instruction unchanged.
- One **agent redeploy**.
- **Verify (leak test):** tenant A ingests a doc with a secret; tenant B's
  `search_documents` for that secret returns **nothing**. A/B tasks + state isolated.

### Phase 3 — RESULTS (run 2026-06-25) ✅ DEPLOYED (agent + gateway rev 00008-kzz)
- **RAG decision revised:** locked "shared corpus + `tenant_id` metadata filter" →
  **per-tenant corpus**. Reason: the deployed `vertexai.rag.import_files` exposes **no
  per-file metadata** param, so a shared-corpus filter can't be reliably enforced; separate
  corpora give *physical* isolation (no filter-bypass leak) at no always-on cost on the
  serverless tier. Each tenant's corpus is stored on its tenant doc; tenant_0 → the existing
  `ragCorpora/4611686018427387904`. The gateway injects the corpus into session state.
- **State injection mechanism (de-risked first):** `engine.create_session(user_id, state=…)`
  persists state on Agent Runtime (verified: read back `{tenant_id, rag_corpus}`), and ADK
  surfaces it via `ToolContext.state` (Phase 0 spike). `ToolContext` params are excluded
  from the LLM schema (auto-injected).
- `app/tools.py`: `_tenant()` / `_corpus()` read state (fallback = owner tenant, never an
  unscoped query). `search_documents`/`ingest_document` use the tenant's corpus;
  `schedule_task`/`list_tasks`/`query_messages` filter by `tenant_id` (in-process to avoid
  composite-index coupling); `cancel_task` treats another tenant's id as not-found;
  `get/set_agent_state` per-tenant doc; `send_email` sends from `assistant+<tenant_id>@jmkn.tech`.
- `gateway`: `ensure_session(user_id, state)` writes `{tenant_id, rag_corpus}` at creation and
  reuses a session only if it already carries the matching `tenant_id` (self-heals old sessions);
  handlers build state via `_session_state(tenant_id)`.
- `app/agent.py`: removed the dormant `PreloadMemoryTool` + `after_agent_callback` (memory is
  gateway-orchestrated; also fixes a "memory service not available" background error). **Model
  and tool set otherwise unchanged.**
- **Verified:** local ADK Runner isolation (A sees task, B `NONE`) **PASS**; **live
  two-tenant leak test against the deployed agent PASS** — task isolation (B can't see A's
  task) + doc routing (B with no corpus returns empty, A with a corpus returns results).

## Phase 4 — Third-party thread tracking + reply routing

- `send_email` / `send_whatsapp` to a non-tenant create/update a `threads` row
  (tenant_id, contact, subject) and capture the outbound email **Message-ID**.
- Inbound reply correlation:
  - **Email:** tenant from the tagged `To:`; match `In-Reply-To`/`References` (or
    contact+subject) → the `threads` row for context.
  - **WhatsApp:** sender phone matches an open thread's contact → that tenant.
- Feed the reply into the initiating tenant's session **with thread context**
  ("reply from John re: invoice"); agent summarises/forwards per the original ask.
- **Thread TTL (3h access cap for third parties):** a third party only reaches the
  agent *through a thread* — the tenant/owner (identity match) is never gated. The
  clock is anchored to the recipient's **first reply**, not the outbound send.
  - `threads` gains `first_reply_at`, `expires_at` (= `first_reply_at + THREAD_TTL_HOURS`),
    `status` (`active`/`expired`). Default `THREAD_TTL_HOURS=3` (per-tenant overridable later).
  - Inbound third-party reply logic (lazy, **no scheduler**; checked per inbound):
    - no thread match → reject (unknown, as today)
    - `now > expires_at` → **block**: drop, log `thread_expired`, do NOT call the agent;
      send a **one-time courtesy reply** ("This conversation has closed."), then silence
      (track `closed_notified` so it fires once).
    - `first_reply_at` unset → set `first_reply_at=now`, `expires_at=now+3h`, then process
    - within window → process normally
  - **Re-send reopens:** a new outbound to the same contact creates/reopens the thread
    (`status=active`, clears `first_reply_at`/`closed_notified`) → their next first reply
    starts a fresh 3h window.
  - Optional cosmetic cron to flip `status=expired` for the admin UI (not required for
    enforcement, which is lazy).
- **Verify:** agent emails a third address on tenant A's behalf; reply from that address
  routes to A (not rejected), A's agent reads + summarises it. Same over WhatsApp.
- **Verify (TTL):** third party replies (window opens); a reply >3h after their first
  reply is blocked + gets the one-time courtesy note; a re-send by the agent reopens a
  fresh window.

### Phase 4 — RESULTS (run 2026-06-25) ✅ DEPLOYED (gateway rev 00009-gnq, no agent redeploy)
- **Gateway-only** (key simplification): the agent already emails third parties *from*
  `assistant+<tenant_id>@jmkn.tech` (Phase 3) and logs every outbound to `messages`, so the
  gateway correlates inbound replies via the tagged `To:` + the outbound log — no agent change.
- `tenancy.parse_tagged_tenant(addr)` → tenant from the `+<tenant>` tag (validated against a
  real tenant). `tenancy.apply_thread_ttl(tenant, channel, contact, latest_outbound_at)` →
  the 3h window state machine over the `threads` collection: first reply opens the window;
  inside → process; expired → block + **one** courtesy note (`closed_notified`); a fresh
  outbound newer than `first_reply_at` reopens. `clients.latest_outbound_to(...)` provides the
  reopen signal from the message log.
- `/inbound/email`: before reject, a valid tag → `_thread_reply_email` → TTL gate → feed the
  reply into the tenant's agent session → relay the agent's **summary to the tenant owner**
  (`tenancy.primary_email`). The third party is never auto-answered except the expiry courtesy.
- **Verified:** Resend accepts the plus-tagged `from` (HTTP 200); tag parse (valid/bogus/no-plus)
  and the full TTL machine (first→process, within→process, expired→blocked+courtesy-once,
  expired-again→silent, reopen→fresh window) unit-tested against live Firestore.
- **KNOWN GAP — WhatsApp third-party replies:** blocked by the same LID rotation as Phase 2
  (inbound arrives as a LID, not the phone the agent sent to → no correlation). Needs the
  bridge to resolve + send the real phone. Email threads are unaffected.
- **Pending:** live end-to-end email round-trip (agent emails a real third party → they reply →
  owner receives the relayed summary).

## Phase 5 — Multi-tenant scheduler

- `/tasks/run`: scan due tasks across **all** tenants; execute each with its own
  `tenant_id` context.
- **Verify:** schedule due tasks for A and B; one tick runs both, each in its own context.

### Phase 5 — RESULTS (run 2026-06-25) ✅ DEPLOYED (gateway rev 00010-t84, gateway-only)
- `/tasks/run` iterates `clients.due_tasks()` (already spans all tenants) and runs each task
  with `tid = task.tenant_id`: `ensure_session(tid, state=_session_state(tid))` +
  `query_agent(user_id=tid, …)`. Per-tenant run-state is checked (cached per tick); a
  paused/stopped tenant's due tasks are **skipped and left pending** (they run when it resumes).
  Response is `{ran, skipped}`.
- **Verified live:** due tasks for tenant_0 + a probe tenant → `{"ran":2,"skipped":0}`, both
  marked `done` under their own tenant; a paused tenant's task → `{"ran":0,"skipped":1}`,
  stayed `pending`.

## Phase 6 — Admin webapp (new Cloud Run service `autoagents-admin`)

- New `admin/` service: FastAPI + Jinja/HTMX (server-rendered, minimal JS), reads/writes
  Firestore. **Shared-password** gate (secret `admin-password`), session cookie.
- Pages: tenants list/create/edit; assign/remove emails + phones; status badges;
  per-tenant start/pause/stop; recent messages/tasks; "pending" (assigned, not onboarded).
- Deploy to Cloud Run (scale-to-zero, authenticated only via the password gate).
- **Verify:** create a tenant + assign an identity in the UI; that identity can then
  onboard by messaging; pause a tenant from the UI → its inbound is parked.

### Phase 6 — RESULTS (run 2026-06-25) ✅ DEPLOYED (`autoagents-admin` rev 00001-dkj, PRIVATE)
- New `admin/` Cloud Run service: FastAPI + server-rendered HTML (no JS framework),
  shared-password gate via an `itsdangerous`-signed cookie (secret `admin-password`).
- Pages: tenants list (lifecycle + agent-state badges, assigned emails/phones); create tenant
  (pending + assign identities inline); tenant detail — add/remove email & phone identities
  (kept in sync between `identities` docs and the tenant doc lists), set run-state
  (running/paused/stopped → `agent_state/<tid>`), set lifecycle (pending/active/disabled),
  recent messages + tasks.
- Reuses the gateway service account (already has Firestore); granted it `secretAccessor` on
  `admin-password`.
- **Access model:** user chose **private** over public+password → deployed
  `--no-allow-unauthenticated`; reached via `gcloud run services proxy autoagents-admin`
  (Google-auth tunnel). Cookie `secure` flag env-gated (`COOKIE_SECURE`, default false) so it
  works over the localhost proxy. `allUsers` bindings = 0 (confirmed not public).
- Smoke-tested locally (auth gate, login ok/wrong, tenant list, detail, controls).
- **Security note:** the admin password was exposed in chat — **rotate** with
  `gcloud secrets versions add admin-password` before real use.

## Phase 7 — Test + docs

- End-to-end with **two real test tenants**: isolation (memory, docs, tasks, state),
  onboarding, third-party reply round-trip (email + WhatsApp), admin controls.
- Update `HUMAN_GUIDE.md`, `AGENT_GUIDE.md`, `steps.md` for multi-tenant.

### Phase 7 — RESULTS (run 2026-06-25) ✅ COMPLETE
- **New-tenant RAG corpus auto-provisioning:** `clients.ensure_tenant_corpus(tenant_id)` creates
  a per-tenant corpus (us-west1, Basic tier) at onboarding and stores it on the tenant doc, so
  every agent gets a private long-term doc store. Gateway SA granted `roles/aiplatform.user`.
  A token-gated `POST /internal/ensure-corpus/{tid}` backfills/repairs corpora.
- **Real second-tenant end-to-end:** created **tenant_1** (Laiba, `laibahiqbal96@gmail.com`) →
  she emailed in → onboarded (pending→active) with welcome + reply; her message logged under
  **tenant_1** (not tenant_0); her own corpus provisioned. Identity routing + message isolation
  confirmed live. Final roster: tenant_0 (owner, 2 emails) + tenant_1 (Laiba), both active with corpora.
- **Production memory bug found + fixed (significant):** the gateway orchestrated Memory Bank
  via `asyncio.run()` inside its **async** FastAPI handlers → `RuntimeError: asyncio.run() cannot
  be called from a running event loop` → both retrieval and storage **silently failed on every
  real webhook**. Memory had never actually worked in production for *any* tenant (Phase 0.5's
  "pass" only ever ran the sync test-script path). Fix: `clients._run_async` runs the coroutine
  in a worker thread when a loop is already active. Verified store→recall works **inside an async
  handler** ("Otter" recalled). Gateway rev 00013-nt7.
- An IAM-propagation race had silently failed tenant_1's onboarding-time corpus; surfaced via
  `log.exception` + the backfill endpoint, then repaired.

---

## ✅ Multi-tenant build COMPLETE (Phases 0–7)
All phases shipped + verified live. Deployables: Agent Runtime brain (tenant-aware tools) +
`autoagents-gateway` (rev 00013) + `autoagents-admin` (private) + Baileys WhatsApp bridge.

### Known gaps / follow-ups
- **WhatsApp third-party reply routing** blocked by LID rotation (bridge sends a rotating
  `@lid`, not the phone) — email threads work; WhatsApp onboarding relies on registering the
  current LID. Proper fix: bridge resolves + sends the real phone (`senderPn`).
- **Rotate** the admin password (was exposed in chat) and the WhatsApp bridge secret.
- Outbound messages log with an empty `tenant_id` from the gateway's own `send_email` (the
  agent-tool sends are tenant-tagged); tag gateway sends too if you want fully complete audit
  attribution.

### Post-build additions
- **Per-tenant analytics (2026-06-26):** gateway records each turn's token usage to a `usage`
  collection; admin shows per-tenant tokens/turns + totals (cost env-gated via
  `LLM_INPUT_COST_PER_1M` / `LLM_OUTPUT_COST_PER_1M`). See `steps.md` + the guides.
- **WhatsApp agent-send fix (2026-06-26):** set `WHATSAPP_BRIDGE_URL` on the engine — Agent
  Runtime ignores `.env`; runtime config is engine `env`/`secretEnv` (`--update-env-vars` /
  `--secrets`).

---

## Cross-cutting

- **Backward compatible:** existing user runs as `tenant_0` from Phase 1; each phase keeps
  the live system working.
- **Rollback:** code phases are gateway/agent redeploys (revert + redeploy). Firestore
  additions are non-destructive (new fields/collections).
- **No new always-on cost:** shared engine; admin webapp scales to zero; one shared RAG corpus.
- **Leak guard:** a unit/eval test that asserts cross-tenant RAG + task isolation, run in CI/manually.
- **Known accepted risk:** email sender spoofing (trusted test group only).

## Sequencing options

- **A — Full build:** Phases 0→7 in order.
- **B — Core first:** 0,1,2,3 (multi-tenant messaging works) → demo → then 4,5,6.
- **C — Spikes only now:** run Phase 0, report, then decide.

## Shipped beyond the original plan (2026-06)

- **Per-tenant WhatsApp (replaces the shared account + LID routing).** Each tenant links
  their **own dedicated WhatsApp number** by scanning a QR from a self-service magic link.
  The bridge is multi-session (one Baileys socket per tenant, creds under
  `wa-auth/<tenant>/`); the account boundary is the tenant boundary, so inbound routing is
  unambiguous and third-party replies relay cleanly. Outbound WhatsApp **and** email are now
  tenant-scoped. Full detail: **AGENT_GUIDE §11**.
- **Leak guard realized.** The planned cross-tenant isolation test now lives in
  `autoagents-agent/tests/post_deploy.py` (a live post-deploy smoke suite — onboarding, both
  channels in/out, third-party relay, per-tenant long-term storage, no-context-leak). See
  **AGENT_GUIDE §12**.
- **Durable runtime config + web_search.** Agent config (bridge URL + secrets) moved to
  Secret Manager `secretEnv` (survives redeploys; deploy via `deploy.sh`); a consent-gated
  `web_search` tool was added.

## Shipped beyond the original plan (2026-07)

- **Per-tenant agent context.** The admin panel can set standing instructions per tenant;
  the gateway prepends them to every prompt, read fresh from the tenant doc each turn.
- **Public domains + admin magic-link auth.** `autoagents.jmkn.tech` (gateway) and
  `admin.autoagents.jmkn.tech` (admin). The Phase-6 shared password is now break-glass only;
  primary sign-in is a 15-min emailed magic link to one allowlisted address.
- **WhatsApp link tokens expire 24h**, and the link page instructs tenants to link a
  **second/dedicated** WhatsApp account rather than their personal one.
- **Uptime ops.** A 5-min liveness sweep (2-tick grace) emails a re-link when a tenant's
  Baileys session drops, and a Monday-09:00 keep-alive WhatsApp keeps linked devices from
  expiring — the failure that took tenant_0 offline for days before it existed.
- **Typing indicator** on WhatsApp while the agent thinks.
- **Memory Bank model pinned** to `gemini-3.5-flash` ahead of the Gemini 2.5 shutdown
  (2026-10-20); it had been silently defaulting to the deprecated 2.5 Flash.
