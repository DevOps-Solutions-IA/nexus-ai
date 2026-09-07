# ADR-0053: Conversation model and unified customer timeline

Status: Accepted. Context: NXS-CUSTOMER-001 requires a channel-neutral conversation context and a unified timeline primitive. Decision:

**Conversation aggregate** — `conversations` is channel-neutral: `channel` (bounded key), `status` (PENDING/OPEN/CLOSED), optional `customer_id`, optional external thread key (`provider_namespace` + `external_thread_id`), bounded subject, timestamps and version. Lifecycle transitions are an explicit state machine (PENDING→OPEN/CLOSED, OPEN→CLOSED, and the explicitly-justified CLOSED→OPEN reopen for later channel phases resuming deterministic external threads); concurrent close is idempotent; invalid transitions fail deterministically (`NXS_CONVERSATION_STATE_CONFLICT`).

**External thread keys** — `UNIQUE(organization_id, channel, provider_namespace, external_thread_id)` with nullable external fields (PostgreSQL treats NULLs as distinct, so the key is enforced exactly when present). Resolution: trusted Organization + channel + external key → the existing Conversation or exactly one new one. No cross-tenant conversation adoption; no global lookup by phone/email.

**Participants** — `conversation_participants` with typed, validated references (CUSTOMER/HUMAN_AGENT/AI_AGENT/SYSTEM). P06 creates only CUSTOMER and SYSTEM participants; HUMAN_AGENT (P17) and AI_AGENT (P13) are typed seams, never fake records. Unique per (conversation, type, ref) — joins are idempotent.

**Timeline** — `conversation_activities` is the unified customer timeline primitive: UUIDv7 ids give deterministic `(occurred_at, id)` ordering (never timestamps alone), cursor pagination, and a nullable `dedup_key` with `UNIQUE(organization_id, dedup_key)` for replay-safe appends. P06 records customer.created, identity.linked, identity.status_changed, conversation.opened/closed and neutral message activity; later phases append their own activity without separate customer histories. The timeline is a domain projection — it is NOT the P04 outbox and NOT the P22 audit log.

**Events** — `customers.created`, `customers.identity.linked`, `customers.identity.status_changed`, `conversations.opened`, `conversations.closed` (all v1, registered, strict payloads, PII-minimal: IDs only), enqueued through the transactional outbox in the SAME transaction as the business mutation — a NATS outage can never corrupt domain state.

**Message boundary** — P06 defines the timeline primitive and the `message.inbound`/`message.outbound` activity types; it does NOT implement channel sending, provider logic, or media handling.

**Indexes** — the identity canonical unique index doubles as the resolution index; per-customer conversation and identity indexes, the external-thread unique index, and the `(organization_id, customer_id, occurred_at, id)` timeline index cover the documented query paths. No premature sharding (P18).
