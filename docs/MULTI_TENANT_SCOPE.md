# autoagents — Multi-Tenant Scope

Scoping the move from a single personal assistant to a **multi-user** system where
each user has "their own agent": isolated memory, documents, tasks, and logs —
reached by messaging from their assigned email/phone — administered via a small webapp.

> **Status: SHIPPED (2026-06-25 → 2026-06-27). This document is a historical record of the
> design decisions, not a to-do.** The system described here is live and serving real tenants.
> For how it actually works today, read `README.md` and `docs/AGENT_GUIDE.md` §10–§12.
>
> **Two locked decisions were later overturned by reality — do not follow this doc on these:**
>
> | Scoped here | Shipped instead | Why |
> |---|---|---|
> | One shared WhatsApp number for all tenants | **One linked WhatsApp account per tenant** (multi-session bridge, self-service QR link) | Inbound arrives as a rotating `@lid`, not the phone — sender-based routing was unresolvable. Making the account the tenant boundary removed the ambiguity entirely. |
> | Shared RAG corpus + `tenant_id` metadata filter | **One RAG corpus per tenant** | The deployed `import_files` has no per-file metadata parameter; separate corpora give physical isolation at no always-on cost. |
>
> Email *is* still a single shared domain routed by sender (`assistant+<tenant>@jmkn.tech`),
> as scoped. Admin auth moved from shared-password-only to **email magic link** (password
> retained as break-glass).
>
> **Open risks (§7) resolved:** both Phase-0 spikes passed (`ToolContext.state` injection works;
> Memory Bank isolates by `user_id`, once it was actually enabled on the engine — it never had
> been). RAG corpus quota is still the resource that grows per tenant. Email spoofing remains
> an **accepted** risk for the trusted beta group.

---

## 1. Conceptual model

- A **tenant** = one user (or account). It owns: identities (emails + phones),
  a document store, a personal memory, conversation sessions, tasks, and a run-state.
- **One shared "brain"** (the existing Agent Runtime engine) serves all tenants.
  Every request carries a `tenant_id`; the agent's tools use it to read/write only
  that tenant's data. Same tools, same abilities for everyone.
- **Routing is by SENDER.** An email from `shahir...@gmail.com` or a WhatsApp from
  `+923070251725` is looked up → tenant → that tenant's isolated context.
- Channels stay **shared**: one inbound email domain (`jmkn.tech`), one WhatsApp
  number. Many tenants, routed by who sent the message — not by separate numbers.

### Why one shared engine (not one engine per user)
| | Shared brain + tenancy (chosen) | Engine per user |
|---|---|---|
| Deploy/update | once | redeploy every tenant on each change |
| Cost | one idle-free engine | N engines (idle-free but quota-bound) |
| Scale | many tenants | bounded by Agent Engine quotas/ops |
| Isolation | by `tenant_id` (sessions/memory/RAG scoped) | hard, but unmanageable |
| Custom per-user logic | no (you want identical) | yes (not needed here) |

---

## 2. What stays the same

- The Agent Runtime engine, the Cloud Run gateway, the WhatsApp bridge, Resend,
  Cloud Scheduler, Secret Manager — all reused.
- The agent's instruction + tool set (send_email, send_whatsapp, schedule_task,
  search_documents, Memory Bank, etc.) — unchanged in *what* they do.
- One email domain, one WhatsApp number.

## 3. What changes (per component)

### 3.1 Firestore data model (new + modified)
- **`tenants`** (new): `{id, name, status: pending|active|paused|stopped,
  emails[], phones[], rag_corpus, created_at, notes}`.
- **`identities`** (new, fast lookup): doc id = normalized identity
  (`email:shahir...@gmail.com` / `phone:923070251725`), value `{tenant_id, channel}`.
  Gateway resolves sender → tenant in one read.
- **`messages`**, **`tasks`**, **`contacts`**: add a `tenant_id` field; all queries
  filter by it. New composite indexes (tenant_id + existing sort fields).
- **`agent_state`**: becomes **per-tenant** (doc id = tenant_id) instead of a singleton.
- **`threads`** (new): tracks conversations the agent starts with **third parties**
  (people who are *not* tenants) so their replies can be routed back. See §3.8.

### 3.2 Gateway routing (`/inbound/email`, `/inbound/whatsapp`)
Routing precedence on each inbound:
1. Sender is a **registered tenant identity** (`identities`) → that tenant (the user).
2. Else, **reply correlation** to a third-party thread (§3.8) → the initiating tenant.
3. Else → **unknown** → reject/ignore (assigned-only; configurable). Optional "not registered" reply.

Then by tenant status:
- **active** → set tenant context (see 3.4) → `query_agent(user_id=tenant_id, …)` → reply.
- **pending** → run **onboarding** (3.5) → welcome message.
- Per-tenant run-state honored (pause/stop checked against that tenant's `agent_state`).

### 3.3 Per-tenant storage (the "separate short/long-term" requirement)
- **Short-term (sessions):** Agent Runtime sessions are already namespaced by
  `user_id`. Set `user_id = tenant_id` → automatic per-tenant isolation. *(free)*
- **Long-term personal (Memory Bank):** Memory Bank scopes memories by `user_id`
  (scope). With `user_id = tenant_id`, recall is naturally per-tenant in the one
  engine. *(free — verify scope param during build)*
- **Long-term documents (RAG):** **shared corpus + `tenant_id` metadata filter**
  (Decision A). `ingest_document` tags each file with the tenant; `search_documents`
  filters retrieval by the current `tenant_id`. One corpus, scales to many tenants.
  Filter is mandatory on every retrieval (guard against leaks).

### 3.4 Tenant-aware tools (the core code change)
- Tools currently read **global config** (RAG_CORPUS, etc.). They must read **tenant
  context** instead. ADK pattern: tools accept a `ToolContext`; the gateway puts
  tenant config into **session state** (`tenant_id`, `rag_corpus`, etc.) when it
  invokes. Each tool reads `tool_context.state["tenant_id"]` → loads tenant → acts:
  - `search_documents` / `ingest_document` → that tenant's corpus (or filter).
  - `schedule_task` / `list_tasks` / `query_messages` → write/read with `tenant_id`.
  - `get_agent_state` / `set_agent_state` → that tenant's state doc.
  - `send_email` / `send_whatsapp` → log with `tenant_id`; recipient as instructed.
- The agent's instruction stays generic; tenancy lives in state, not the prompt.

### 3.5 Onboarding flow
1. Admin (webapp) creates a tenant + assigns emails/phones → status `pending`.
2. User sends the first message from an assigned identity.
3. Gateway sees pending → provision: create the tenant's RAG corpus (if per-tenant),
   init `agent_state`, status → `active`, send a welcome reply.
4. Later messages → normal agent flow.

### 3.6 Scheduler (`/tasks/run`)
- Scan due tasks across **all** tenants; execute each with its own `tenant_id`
  context (user_id=tenant_id). Reply/act per tenant.

### 3.7 Admin webapp (new)
- New small Cloud Run service `autoagents-admin` (FastAPI + lightweight HTML/HTMX,
  reads/writes Firestore). Features:
  - CRUD tenants; assign/remove emails + phones; see status.
  - Per-tenant: start/pause/stop, view recent messages/tasks, resend welcome.
  - "Pending" list (assigned, not yet onboarded).
- Auth required (see Decision C). Separate service so the public gateway stays minimal.

### 3.8 Reading replies from third parties (thread routing)
When the agent contacts someone who is **not** a tenant (e.g. "email John for an
update") and they reply, the reply must route back to the tenant who started it.

- **`threads`** collection: `{id, tenant_id, channel, contact (email/phone),
  subject, message_id (email), session_id, status, last_at}`.
- On `send_email` / `send_whatsapp` to a third party, the tool records a thread row
  (tenant_id + contact + the outbound email **Message-ID**).
- On inbound, after the sender fails the tenant-identity check, try **reply correlation**:
  - **Email (chosen):** the agent contacted John from `assistant+t<tenant>@jmkn.tech`,
    so John's reply `To:` carries the tenant tag → tenant resolved from the address
    (spoof-proof). Then match `In-Reply-To` / `References` (or contact + subject) to the
    specific `threads` row for conversation context.
  - **WhatsApp:** the sender's phone matches an open thread's `contact` → that tenant.
- The reply is fed into the **initiating tenant's** session with context ("reply from
  John re: invoice"), so the agent can summarise/forward to the tenant — which is exactly
  the "take follow-ups from others and summarise to me" requirement.

Caveats:
- Email Message-ID correlation is the reliable path; a **tagged reply address** is even
  stronger (immune to sender spoofing). → Decision F.
- WhatsApp has one number and no addressing trick: if two tenants messaged the same
  phone, a reply is ambiguous → resolve to the most-recent open thread (rare edge case).
- Threads expire/close (e.g. after N days) so stale contacts don't route forever.

---

## 4. Security considerations

- **Sender spoofing (email):** `From:` is forgeable. Routing by email sender means a
  forged `From: shahir...@gmail.com` could reach that tenant. Mitigation: check inbound
  **SPF/DKIM pass** (Resend provides auth results) before trusting the sender; reject on
  fail. WhatsApp numbers are far harder to spoof. → Decision B.
- **Assigned-only:** reject unknown senders so randoms can't spin up tenants / burn cost.
- **Cross-tenant leakage:** tools must *always* scope by `tenant_id`; a missing filter
  leaks data. Per-tenant corpus (hard isolation) reduces this risk vs shared+filter.
- **Admin webapp auth:** must be locked down (it controls everyone's agents).
- **Secrets:** unchanged; per-tenant API keys not needed (shared channels).

---

## 5. Locked decisions

- **A — RAG isolation → SHARED corpus + `tenant_id` metadata filter.** One corpus;
  every ingested doc tagged `tenant_id`; `search_documents` filters by the current
  tenant. Scales to many tenants. *Implication:* tools MUST always apply the filter —
  a missing filter leaks across tenants (guard + test this hard).
- **B — Email trust → TRUST SENDER AS-IS** (no SPF/DKIM enforcement for now).
  *Implication:* a forged `From:` could impersonate a tenant. Acceptable for a trusted
  test group; revisit before opening up. (Tagged reply addresses below mitigate the
  third-party-reply path, but not the tenant's own first contact.)
- **C — Admin webapp auth → SHARED PASSWORD** (stored in Secret Manager). Simple gate
  on the admin Cloud Run service.
- **D — Unknown senders → REJECT** (assigned-only). Admin assigns identities first;
  only registered identities (or correlated third-party replies) are processed.
- **E/F — Outbound identity + reply routing → TAGGED REPLY ADDRESS.** Agent sends on a
  tenant's behalf from `assistant+t<tenant_id>@jmkn.tech`; replies carry the tenant in
  the `To:` address (Resend receiving catches all `@jmkn.tech`; parse the `+tag`).
  Spoof-proof routing, shared domain, no per-tenant Resend setup. WhatsApp (one number)
  uses the `threads` contact-phone match.

---

## 6. Effort / phasing (rough)

1. **Data model + identity routing** (Firestore tenants/identities, gateway sender→tenant,
   reject unknown). — core, moderate.
1b. **Third-party thread tracking** (`threads` collection; capture Message-ID on send;
   reply correlation on inbound; feed replies into the initiating tenant). — moderate;
   also benefits single-tenant follow-ups. Needed for "read replies from people I contacted".
2. **Tenant-aware tools** (ToolContext + session state threading; per-tenant Firestore
   writes/reads; per-tenant agent_state). — moderate, touches every tool + a redeploy.
3. **Per-tenant RAG** (corpus-per-tenant or metadata filter) + Memory Bank/session
   verification. — small–moderate.
4. **Onboarding flow** (pending → provision → active → welcome). — small.
5. **Scheduler multi-tenant** (per-tenant task routing). — small.
6. **Admin webapp** (new Cloud Run service, CRUD + controls + auth). — moderate, standalone.

No new always-on cost (shared engine; webapp scales to zero). Per-tenant RAG corpora
are the only resource that grows with tenants — bounded by Decision A.

---

## 7. Open risks

- Memory Bank per-`user_id` scoping + ADK `ToolContext.state` injection are the two
  assumptions to validate first (quick spikes) before the big refactor.
- RAG Engine corpus quota if per-tenant corpora and many tenants.
- Email spoofing if SPF/DKIM not enforced.
- `create-with-container` deprecation already noted for the WhatsApp VM.
