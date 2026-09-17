# ADR 0098: Human operations authority and fencing

- Status: Accepted for NXS-P17
- Date: 2026-09-17
- Requirement: `NXS-HUMAN-001`

## Context

Human operators and AI agents may act on the same tenant conversation at different times.
Browser connectivity, WebSocket presence and process-local locks cannot establish customer-facing
authority. Claims, transfers, supervisor actions, human messages and return-to-AI requests must
remain deterministic across replicas, crashes and retries without holding database transactions
open across P09 or P13 calls.

## Decision

PostgreSQL is authoritative for queues, explicit agent presence, bounded capacity, work items,
assignments, conversation ownership, handoffs, action authorizations and append-only transition
history. Every tenant row carries `organization_id`, uses forced RLS and references tenant-owned
resources through composite foreign keys.

Authority mutations use the global lock order: queue, ascending agent-presence rows, work item,
conversation ownership, assignment, handoff and action authorization. Claims take the queue
`FOR SHARE`, lock capacity, select eligible work with `FOR UPDATE SKIP LOCKED`, create one fresh
opaque claim token and increment the work lease and conversation ownership generations. Queue
configuration takes `FOR UPDATE` and increments its revision, making disable/configuration races
first-commit deterministic.

Conversation ownership has exactly `AI`, `HUMAN` or `UNASSIGNED` mode. Every customer-facing
action validates the current ownership generation. AI-to-human handoff increments that generation,
creates an idempotent work item and fences stale P13 output. Human acceptance grants `HUMAN`
authority only after the assignment token and lease are current.

Human-to-AI return is two-stage. P17 first persists `AI_RETURN_PENDING`, changes ownership to
`UNASSIGNED`, releases human capacity and records one stable P13 identity. It then invokes P13
outside the transaction. `AI` ownership is committed only after the exact same-tenant P13 session,
agent and conversation are accepted and bound. Deterministic rejection requeues the work; possible
acceptance with a lost result remains pending/ambiguous for P25 and never restores human authority
or creates a new P13 identity.

Human messages use a durable P17 action authorization containing the assignment token version,
ownership generation, semantic fingerprint and stable P09 idempotency key. Its commit is the
logical authorization point. P17 then calls `MessagingService`; it never calls a provider adapter.
Transfer or supervisor release committed first fences the message, while authorization committed
first may proceed using the already-established logical authority.

Copilot execution uses P13 and returns advisory text only. P11/P12 call and voice identifiers are
tenant-scoped references; P17 does not own media, SIP or provider transport.

## Consequences

- Physical exactly-once delivery or AI execution is not claimed; stable logical identities and
  durable fencing are.
- Browser disconnect does not release work or change presence automatically.
- No lease timeout silently steals active work.
- P25 owns stale-assignment recovery and ambiguous P09/P13 reconciliation.
- Human actions, supervisor mutations and P04 outbox intent commit atomically with business state.
