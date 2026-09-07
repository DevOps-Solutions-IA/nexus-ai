# Data and Event Platform (NXS-P04)

The permanent enterprise event foundation. Later phases build on it; P04 implements no
business domains.

## Delivery semantics

Nexus AI guarantees **at-least-once** event transport. **Exactly-once transport is NOT
claimed.** Duplicate-safe business effects come from **idempotent consumers**:

> at-least-once transport + idempotent consumption = effectively-once business effect.

See [ADR-0047](../adr/0047-event-delivery-semantics.md).

## The envelope

`nexus_ai.events.EventEnvelope` — one immutable, versioned structure for every business
event (`schema_version` `1.0`). Key fields: `event_id` (UUIDv7), `event_type`
(namespaced, dotted), `event_version`, `occurred_at` (UTC), `scope` (`tenant`/`global`),
`organization_id` (trusted, server-side), `aggregate_type`/`aggregate_id`,
`correlation_id`/`causation_id`/`trace_id`, `producer`, `payload`, `metadata`.

Build with `EventEnvelope.create(...)`; `organization_id` decides the scope. Parse
inbound bytes with `EventEnvelope.from_json` (raises `EventContractError`).

## Subjects

`nxs.<environment>.<scope>.<domain>.<event>`, e.g.
`nxs.production.tenant.organizations.profile.updated`. Built and parsed only through
`nexus_ai.events.subjects`. Subjects never carry tenant ids or PII; publish subjects
reject wildcards. See [ADR-0049](../adr/0049-event-envelope-and-subjects.md).

## Producing an event (future phases)

```python
envelope = EventEnvelope.create(
    event_type="organizations.created",
    event_version=1,
    aggregate_type="organization",
    aggregate_id=str(organization.id),
    producer=settings.service_name,
    payload={"organization_key": organization.organization_key},
    organization_id=organization.id,
)
async with database.tenant_transaction(organization.id) as tenant:
    ...  # the business mutation
    await resources.event_platform.publisher.enqueue(tenant.session, envelope)
# the outbox row and the mutation commit together; the relay publishes later
```

Global platform events: `await event_platform.publisher.publish_global(envelope)`.

## Transactional outbox

`event_outbox` (tenant-owned, forced RLS). The row and the business mutation commit in
one transaction. `OutboxRelay` workers claim rows with `FOR UPDATE SKIP LOCKED`, take a
lease, publish to JetStream, check the ack, then mark `PUBLISHED`. A crashed worker's
lease expires and the row is reclaimed. A committed row survives a full NATS outage.
See [ADR-0048](../adr/0048-transactional-outbox.md).

## Consuming events (future phases)

```python
async def handle(ctx: EventContext) -> None:
    # ctx.session is already bound to ctx.envelope's tenant scope (RLS enforced)
    ...


consumer = event_platform.register_consumer(
    ConsumerSpec(
        name="p06-conversation-projector",
        subject_filter="nxs.production.tenant.organizations.>",
        handler=handle,
        event_types=frozenset({"organizations.created"}),
    )
)
```

The framework: explicit ack only after the handler effect and the idempotency receipt
commit; retryable failures NAK with bounded exponential backoff; terminal failures are
recorded, dead-lettered and TERMed — never hot-looped; bounded concurrency, ack-wait and
handler timeout; graceful drain on stop.

## Idempotency

`consumer_receipts` (platform-internal). `ConsumerReceiptStore.claim` inserts a receipt
`ON CONFLICT DO NOTHING`; a losing racer takes a `FOR UPDATE` lock and, on seeing
`PROCESSED`/`DEAD`, acknowledges without re-running the handler. A lost ACK, a redelivery
and two concurrent deliveries all resolve to exactly one effect.

## Dead letters

`event_dead_letters` (tenant-owned) for tenant events, plus the `nxs.<env>.dlq.>` stream.
Retains identity, failure class, attempt count and a **sanitized** summary (stable title
/ type — never an exception message or stack trace). Replay is controlled and audited
(`python -m scripts.nxs_events replay`).

## Operational recovery

| Symptom | Where to look | Action |
| --- | --- | --- |
| Events not arriving | `event_outbox` rows stuck `PENDING`/`FAILED` | check `last_error_code`; is JetStream up? is the relay running? |
| Row stuck `PUBLISHING` | worker crashed mid-lease | the lease expires (`publisher_lease_seconds`) and another worker reclaims |
| `DEAD` outbox rows | `event_dead_letters` (origin `OUTBOX_PUBLISH`) | fix the cause, then replay |
| Handler failing repeatedly | consumer logs `event_processing_retry` | inspect `error_code`; after `max_delivery_attempts` it dead-letters |
| Poison message | `event_dead_letters` (origin `CONSUMER`), receipt `DEAD` | fix the handler/contract, then replay the dead letter |
| Readiness `503` on `event_platform` | `last_error_code` | `jetstream_unavailable` or `stream_topology_missing` — restore NATS / re-run topology bootstrap |

## Configuration

`NXS_EVENTS__*` — see `nexus_ai.core.config.EventsSettings`. Hardened environments
require `REQUIRE_JETSTREAM=true` and refuse an unsafe polling cadence.
