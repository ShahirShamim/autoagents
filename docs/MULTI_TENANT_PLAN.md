# autoagents — Multi-Tenant Implementation Plan

Concrete build plan for the multi-tenant transformation. Decisions are locked in
`MULTI_TENANT_SCOPE.md` (shared corpus + tenant_id filter; tagged reply address;
shared-password admin webapp; trust email sender; reject unknown).

Each phase lists **changes**, **new files/resources**, and a **verify** gate.
Phases are independently shippable; the system keeps working for the existing user
throughout (migrated as the first tenant). Nothing deploys without your OK.

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

## Phase 5 — Multi-tenant scheduler

- `/tasks/run`: scan due tasks across **all** tenants; execute each with its own
  `tenant_id` context.
- **Verify:** schedule due tasks for A and B; one tick runs both, each in its own context.

## Phase 6 — Admin webapp (new Cloud Run service `autoagents-admin`)

- New `admin/` service: FastAPI + Jinja/HTMX (server-rendered, minimal JS), reads/writes
  Firestore. **Shared-password** gate (secret `admin-password`), session cookie.
- Pages: tenants list/create/edit; assign/remove emails + phones; status badges;
  per-tenant start/pause/stop; recent messages/tasks; "pending" (assigned, not onboarded).
- Deploy to Cloud Run (scale-to-zero, authenticated only via the password gate).
- **Verify:** create a tenant + assign an identity in the UI; that identity can then
  onboard by messaging; pause a tenant from the UI → its inbound is parked.

## Phase 7 — Test + docs

- End-to-end with **two real test tenants**: isolation (memory, docs, tasks, state),
  onboarding, third-party reply round-trip (email + WhatsApp), admin controls.
- Update `HUMAN_GUIDE.md`, `AGENT_GUIDE.md`, `steps.md` for multi-tenant.

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
