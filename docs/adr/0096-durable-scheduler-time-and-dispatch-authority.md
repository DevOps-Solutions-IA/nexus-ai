# ADR-0096: Durable Scheduler Time and Dispatch Authority

Status: Accepted for NXS-P15

## Context

Nexus AI needs one-time and recurring activation of immutable P14 workflow versions across many backend replicas. Host cron, process-local timers, and worker clocks cannot provide tenant isolation, deterministic DST behavior, duplicate prevention, or durable cancellation ordering. P15 must not become a second workflow, tool, agent, or provider runtime.

## Decision

PostgreSQL owns schedule cursors, database-time due ordering, materialized occurrences, claims, owner/token fencing, revisions, cancellation, and idempotency. All scheduler tables carry `organization_id`, forced RLS, and tenant-aware composite foreign keys. Materialization is just in time and serialized with `FOR UPDATE SKIP LOCKED`; a versioned bounded key derived from schedule UUID, schedule revision, IANA timezone identity, canonical local slot, and fold is protected by the unique tenant/schedule/occurrence constraint. Claims persist owner and opaque token before dispatch, and terminal writes compare the expected state, owner, and token.

The only target is `START_WORKFLOW` against an immutable, same-tenant P14 workflow version. P15 calls `WorkflowService.start_run` with a stable persisted idempotency key. It never calls P08, P13, a provider, shell, SQL, arbitrary HTTP, or infrastructure directly. P14 remains the sole workflow execution and step-retry authority.

Recurrence is a closed structured contract: minutely, hourly, daily, weekly, or monthly with positive bounded intervals and validated calendar selectors. UTC timestamps are execution authority; IANA timezone, intended local time, UTC offset, fold, and timezone-data version are retained. Nonexistent spring-forward slots become `SKIPPED`; fall-back uses fold zero once. Misfires are explicitly `SKIP`, `FIRE_ONCE`, or bounded catch-up. Search, catch-up, and tick batches have hard ceilings. Elapsed slots omitted by those bounds are never silently erased: append-only scheduler transition history stores one deterministic revision/timezone/range-bound summary in the same transaction as bounded occurrence materialization and cursor advancement.

A durable `CLAIMED` occurrence left by a crashed worker remains ambiguous. P15 records enough evidence to diagnose it but does not automatically reap, reassign, or reconcile it. P25 owns that recovery authority.

## Consequences

- Multiple scheduler replicas can contend without shared memory.
- Cancellation, pause, edit, materialization, and claim races have database commit order.
- One occurrence maps to at most one logical P14 run, without claiming physical exactly-once effects.
- Materialized temporal intent never changes when a schedule or timezone rule changes.
- A paused schedule edit may deliberately revisit the same canonical local slot under a new revision without colliding with the immutable prior-revision occurrence.
- Operators need P25 for dead-owner and ambiguous-dispatch reconciliation.
- P15 certification is limited to **SCHEDULER CONTRACT + DURABILITY CERTIFIED**.

## Current Versus Target

NXS-P15 implements the schema, strict contracts, recurrence engine, lifecycle, JIT materialization, misfires, claim/dispatch fencing, P14 integration, API, RBAC, P04 events, and adversarial validation. P16 owns campaigns, P24 full observability, P25 recovery/failover reconciliation, and P32 deployment.
