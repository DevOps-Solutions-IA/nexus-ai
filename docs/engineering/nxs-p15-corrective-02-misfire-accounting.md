# NXS-P15 Corrective #2 — Misfire Accounting

## Finding and fail-first evidence

Canonical `materialize_due()` selected only the latest elapsed slot for `SKIP` and `FIRE_ONCE`, or the first `max_catch_up` slots for `CATCH_UP_BOUNDED`, then advanced the schedule cursor past every elapsed slot. The omitted logical slots had no durable occurrence or summary. A real-PostgreSQL fail-first test observed one `FIRE_ONCE` occurrence and zero `MISFIRE_FIRE_ONCE_COALESCED` history records.

## Corrected semantics

- `SKIP` materializes the newest elapsed slots as terminal `SKIPPED` occurrences up to the remaining tick batch. The latest elapsed slot is always represented. Older excess slots are summarized once with `MISFIRE_SKIPPED`; no dispatchable occurrence is created.
- `FIRE_ONCE` materializes exactly one dispatchable occurrence for the latest elapsed slot. Earlier slots are summarized once with `MISFIRE_FIRE_ONCE_COALESCED`.
- `CATCH_UP_BOUNDED` materializes the oldest elapsed slots up to both `max_catch_up` and the remaining tick batch. Excess slots are summarized once with `MISFIRE_CATCH_UP_COALESCED`.

The default batch remains 100, the hard batch ceiling remains 500, catch-up remains at most 100 and recurrence search remains capped by `MAX_SEARCH_STEPS`.

## Durable accounting model

The existing append-only, forced-RLS `scheduler_transition_history` is the scheduler-owned accounting store. A summary is a same-state `SCHEDULE` transition whose canonical JSON detail contains:

- deterministic `accounting_key`;
- policy, disposition and schedule revision;
- skipped/coalesced/accounted counts;
- first and last omitted local slots, UTC instants and fold values;
- IANA timezone and timezone-data version.

The identity is a SHA-256 digest of organization, schedule, revision, policy, timezone and the exact first/last omitted local-slot range. It uses no process-random hash. Materialization holds the schedule row lock, checks for the same durable detail before insert, writes occurrences and the summary, and only then advances the cursor in the same PostgreSQL transaction. A rollback therefore preserves the old cursor and removes partial occurrences/accounting. A retry reconstructs the same key; concurrent workers serialize on the schedule row.

No schema migration is required because transition history already has immutable `detail`, tenant ownership, schedule identity, forced RLS and append-only mutation guards.

## DST and revisions

Nonexistent spring-forward occurrences remain `SKIPPED` with `DST_NONEXISTENT_LOCAL_TIME`. Other elapsed slots in the same window retain normal misfire treatment. Accounting identity includes the schedule revision and timezone identity, so an edited/resumed revision may safely account for the same wall-clock range without colliding with or mutating prior evidence.

## Certification boundaries

This corrective changes only P15 Scheduler materialization, identities, repository logic, tests, documentation and P15 evidence. It does not modify P16 runtime, start P17, provide P25 orphan recovery, claim global physical exactly-once execution or deploy any environment.
