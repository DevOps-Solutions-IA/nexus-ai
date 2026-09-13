# NXS-P15 Corrective #1 — Revision-Safe Occurrence Identity

## Audit finding and fail-first evidence

External review found that P15 built `occurrence_key` from only the schedule UUID and minute-level intended local slot, with a hard-coded fold. The key omitted both schedule revision and IANA timezone identity even though the approved P15 contract required them.

The corrective was reproduced against reviewed head `430ef8665d95684e6ef7c064991d9f834778c8e5` with a real PostgreSQL integration test. A recurring schedule materialized a due slot, transitioned through `PAUSED`, was edited (incrementing its optimistic revision and clearing its cursor), resumed, and revisited the same canonical local slot. The second materialization failed at `SchedulerRepository.add_occurrence` with `UniqueViolationError` on `uq_scheduler_occurrences_identity`. This classified the defect as a production identity bug rather than a test race.

## Corrected identity

`build_occurrence_key` creates this bounded external representation:

```text
v1:r<schedule_revision>:f<fold>:<sha256-canonical-payload>
```

The SHA-256 input is deterministic sorted compact JSON containing:

- schedule UUID;
- positive schedule revision;
- exact IANA timezone identity;
- canonical intended naive local wall-clock timestamp at microsecond precision;
- fold (`0` or `1`).

The representation is non-secret, stable across processes and restarts, independent of Python's randomized `hash()`, and remains within the existing 160-character column. No schema migration is required. PostgreSQL uniqueness on `(organization_id, schedule_id, occurrence_key)` remains the final authority: the same revision/local slot/timezone/fold collides, while a new revision may legitimately revisit that slot.

## Edit, resume, immutability, and dispatch

`PAUSED -> edit -> revision increment -> cursor reset -> resume -> bounded misfire evaluation` now creates a distinct occurrence identity when the recalculated cursor revisits a prior slot. State transitions also increment the optimistic schedule revision under the existing lifecycle contract, so each materialized occurrence snapshots the exact current revision.

Existing occurrence rows remain protected by the P15 immutable-occurrence trigger. Their revision, workflow version, workflow input, timezone, misfire policy, intended slot, and occurrence key do not change. P14 dispatch idempotency is unchanged: each occurrence retains its persisted `schedule:<schedule_uuid>:<occurrence_uuid>` key, and replay of one occurrence still resolves to one logical P14 run.

## DST and concurrency

Spring-forward gaps remain explicit `SKIPPED` occurrences and fall-back still materializes fold zero once. Rebuilding an identity for the same revision and DST slot is deterministic; fold is part of the canonical identity even though current policy emits only fold zero for ambiguous fall-back slots.

Two materializers still serialize on the schedule row with `FOR UPDATE SKIP LOCKED`. The database unique constraint rejects a second insert for an identical same-revision slot even if repository code is called directly. A revision-N occurrence and a revision-N+1 occurrence for the same local slot are distinct and valid.

## Scope and remaining boundary

This corrective changes only P15 occurrence identity and its certification tests/documentation. It does not add stale-claim reassignment, orphan recovery, ambiguous-dispatch reconciliation, or failover; those remain NXS-P25 responsibilities. NXS-P16 is not activated, and no deployment is performed.
