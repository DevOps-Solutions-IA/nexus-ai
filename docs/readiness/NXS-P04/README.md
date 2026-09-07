# NXS-P04 Readiness — Data and Event Platform

Branch `feat/nxs-p04-data-events`, dependency NXS-P03 READY/GO (merged to `main` at
`d95e0356b897adf9cebf8b42765412e4b19e9b86`). Machine-readable evidence in
`.nxs/evidence/NXS-P04/` is authoritative for automated gates; this page is the
engineering summary for independent audit.

## Delivery semantics

Nexus AI guarantees **at-least-once** event transport. **Exactly-once transport is NOT
claimed.** Duplicate-safe business effects come from **idempotent consumption**:
at-least-once transport + idempotent consumption = effectively-once business effect.
See [ADR-0047](../../adr/0047-event-delivery-semantics.md),
[ADR-0048](../../adr/0048-transactional-outbox.md),
[ADR-0049](../../adr/0049-event-envelope-and-subjects.md) and
[docs/engineering/events.md](../../engineering/events.md).

## Architecture

- **Canonical envelope** `nexus_ai.events.EventEnvelope` (`schema_version` `1.0`,
  immutable, `extra="forbid"`): UUIDv7 `event_id`, namespaced `event_type`, explicit
  `event_version`, UTC `occurred_at`, `scope` (`tenant`/`global`), trusted
  `organization_id`, aggregate identity, `correlation_id`/`causation_id`/`trace_id`,
  `producer`, `payload`, `metadata`.
- **Subject taxonomy** `nxs.<environment>.<scope>.<domain>.<event>` — built/parsed only
  through `nexus_ai.events.subjects`; injection-safe; wildcards rejected in publish
  subjects; no tenant id or PII in subjects.
- **Schema/version registry** `nexus_ai.events.registry` — typed Pydantic payload
  models keyed by `(event_type, version)`; unknown types and unsupported versions fail
  closed; payloads are `extra="forbid"`.
- **Transactional outbox** `event_outbox` (TENANT-OWNED, forced RLS + `nxs_system_relay`
  unscoped policy). The row and the business mutation commit in one transaction;
  `WITH CHECK` makes a forged `organization_id` impossible. `OutboxRelay` workers claim
  with `FOR UPDATE SKIP LOCKED`, take a crash-recovering lease, publish to JetStream,
  check the ack, then mark `PUBLISHED`.
- **JetStream transport** — extends the P01 messaging boundary; every publish ack
  checked under a bounded timeout; `event_id` is the broker dedup identity; no silent
  fallback to core NATS; hardened environments fail closed without durable JetStream.
- **Durable consumer framework** — pull consumer, explicit ack only after the handler
  effect and receipt commit, bounded ack-wait / retry / concurrency / handler timeout,
  graceful drain.
- **Consumer idempotency** `consumer_receipts` (PLATFORM-INTERNAL, no RLS) —
  `(consumer_name, event_id)` claim in the same transaction as the handler effect.
- **Dead letters** `event_dead_letters` (TENANT-OWNED) + `nxs.<env>.dlq.>` stream —
  identity, failure class, attempt count and a **sanitized** summary (stable title —
  never an exception message or stack trace). Replay via `python -m scripts.nxs_events`
  is manual and audited; nothing replays automatically or forever.

## Database roles and classification

- `nexus_migration` owns the three new tables; `nexus_runtime` gets `SELECT/INSERT/
  UPDATE` only — **never DELETE**. Retention is a migration-role administrative job.
- `event_outbox` / `event_dead_letters` — TENANT-OWNED, `ENABLE` + `FORCE ROW LEVEL
  SECURITY`, `nxs_tenant_isolation` (`USING` + `WITH CHECK`) plus `nxs_system_relay`
  (`USING`/`WITH CHECK` = unscoped only). A tenant request always binds a scope so it
  can never use the relay policy.
- `consumer_receipts` — PLATFORM-INTERNAL, no RLS (holds no tenant business content,
  must also cover global events, addressed only by exact key).
- The tenant schema guard now derives tenancy from the ORM class hierarchy
  (`TenantOwnedMixin`), closing a latent gap where the P03 tenant tables carried the
  `organization_id` column and RLS but were not enumerated by the guard.

## Failure / concurrency / security matrix (real PostgreSQL + NATS/JetStream)

| Scenario | Result |
| --- | --- |
| Business mutation + outbox enqueue commit atomically | proven |
| Business failure rolls back the outbox row | no event from uncommitted state |
| Forged `organization_id` on enqueue | rejected by RLS `WITH CHECK` |
| Same `event_id` enqueued twice | idempotent no-op |
| NATS unavailable when the relay runs | rows never `PUBLISHED`, retried, then delivered |
| Publish attempts exhausted | outbox `DEAD` + durable dead-letter row |
| 6 relay workers over one population | each row `PUBLISHED` exactly once |
| Crashed worker holding a `PUBLISHING` lease | lease expires, another worker reclaims |
| Duplicate delivery / lost ACK / redelivery | exactly one business effect |
| Two concurrent deliveries of one event | exactly one business effect |
| Retryable handler failure | bounded exponential backoff, then success |
| Terminal handler failure | dead-lettered, receipt `DEAD`, TERMed, redelivery acked |
| Malformed envelope / unknown type / unsupported version | quarantined, no handler run |
| Subject injection / wildcard in a publish subject | rejected by the subject builder |
| Payload `organization_id` disagrees with the envelope | terminal (`EventTenantScopeError`) |
| Cross-tenant read of another tenant's `event_outbox` | blocked by RLS |
| Concurrent handlers for different tenants | each sees only its own Organization |
| Exception message in logs or the durable dead-letter record | never present |

## Preserved P01–P03 guarantees

Runtime/migration role separation, non-bypass runtime, forced RLS, authenticated tenant
resolution, live session/user/membership/org validation, org-scoped RBAC, secret
redaction, structured errors/logs, async context isolation, health/readiness semantics,
graceful shutdown, phase-neutral CI, clean-room, amd64/arm64 — all unchanged and
regression-tested.

## Scope preserved for later phases

NXS-ORG-001 stays PLANNED / NXS-P05. No provisioner, dashboards, customers, Integration
Hub, Tool Engine, channels, telephony, agent runtime, workflows, Sentinel, compliance,
audit platform, billing or full observability. External webhooks remain an untrusted
P07+ ingestion boundary — P04 provides the provenance seam only.
