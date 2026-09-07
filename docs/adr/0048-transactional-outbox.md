# ADR-0048: Transactional outbox and dead-letter classification

Status: Accepted (NXS-P04).

Context: "Commit the database, then publish to NATS" is two independent operations: a crash or a NATS outage between them either loses the event or emits an event for state that rolled back. The core rule (`no business event is emitted from uncommitted DB state; DB state and event intent are atomic`) forbids this.

Decision:

- **`event_outbox` (TENANT-OWNED, forced RLS).** A tenant business event is written to `event_outbox` **inside the same transaction** as the business mutation. `WITH CHECK` on `nxs_tenant_isolation` guarantees the row's `organization_id` equals the bound tenant scope, so a forged `organization_id` in a payload cannot poison the outbox. The row's primary key is the `event_id`, so enqueuing twice is a no-op.
- **Relay.** One or more `OutboxRelay` workers run in an explicitly **unscoped** system transaction (the `nxs_system_relay` policy, visible only when no tenant scope is bound — a tenant request can never use it). They claim rows with `SELECT ... FOR UPDATE SKIP LOCKED`, take a time-boxed `PUBLISHING` lease, publish to JetStream, check the acknowledgement, and only then mark the row `PUBLISHED`. A crashed worker's lease expires and another worker reclaims the row — no row is permanently stuck.
- **Global events** carry no tenant transaction to be atomic with and are published directly through the same confirmed JetStream path.
- **Backoff and dead-letter.** A transient publish failure reschedules the row with bounded exponential backoff (`retry_base` → `retry_max`). After `max_publish_attempts` the row moves to `DEAD` and — for tenant events — a durable `event_dead_letters` row is written and a message is emitted to the dead-letter subject. `event_dead_letters` retains identity, failure classification, attempt count and a **sanitized** summary (the stable error title/type, never the exception message or a stack trace). Replay is a controlled, audited operation.
- **No unrestricted DELETE.** The runtime role has `SELECT/INSERT/UPDATE` only on all three tables. Retention is an administrative job run by the migration role.
- **`consumer_receipts` (PLATFORM-INTERNAL, no RLS).** Idempotency bookkeeping only — consumer name, globally unique event id, outcome, timestamps. It holds no tenant business content, is only ever addressed by exact key, and must also cover global-scoped events that have no `organization_id`.

Consequences: a committed tenant event is delivered at least once even across a total NATS outage. The relay is horizontally scalable. The dead-letter path makes poison messages visible and recoverable without automatic infinite retry.
