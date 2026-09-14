# NXS-P16 Corrective #2: Scheduled-release bridge

## Audit finding

At reviewed head `5e724108225e7a04baf1d23fbcfc1524684a3cc4`, P16 created and bound a
governed P15 schedule, and P15 durably persisted the exact P14 workflow run on its
occurrence. P16 did not copy that exact identity into
`campaign_runs.release_workflow_run_id`. Consequently `confirm_release` rejected the
scheduled path with `release workflow has not started` even after P14 completed.

The fail-first PostgreSQL test executed schedule creation, occurrence materialization,
claim, P15-to-P14 dispatch and P14 completion. It reproduced that exact
`CampaignInvalidStateError` before production code changed.

## Corrected authority chain

```text
P16 campaign run + owned P15 schedule
  -> P15 materialized occurrence
  -> P15 claim and generic P14 dispatch
  -> P15 occurrence DISPATCHED with exact workflow_run_id
  -> P16 scheduled-release handler
  -> durable occurrence + workflow-run binding on the existing campaign run
  -> exact P14 run COMPLETED
  -> P16 confirm_release
  -> bounded recipient-attempt materialization
```

P15 remains the temporal and occurrence authority. P14 remains the generic workflow
authority. The P16 application service is the only component that interprets the bounded
campaign identifiers carried in the P15 schedule input.

## Durable binding and fences

`campaign_runs.release_schedule_occurrence_id` is a nullable, tenant-aware foreign key to
`scheduler_occurrences`. It is unique per Organization. A database check requires a bound
occurrence to have both the existing schedule ID and P14 release-run ID.

Before the first binding, P16 re-reads and verifies:

- trusted Organization scope;
- campaign, immutable revision and existing campaign-run IDs;
- the P16-owned schedule ID;
- the exact dispatched occurrence ID;
- occurrence and P14 workflow-run identity equality;
- release workflow version equality across revision, schedule, occurrence and workflow run;
- the closed campaign/revision/run input map on both schedule and workflow run;
- a `SCHEDULED` campaign and `PENDING_RELEASE` run.

The binding transaction locks campaign then run, writes occurrence and workflow-run IDs
together, and appends `SCHEDULED_RELEASE_BOUND` history. Equal-pair redelivery is an
idempotent success and emits no duplicate transition. A different pair, tenant, schedule,
version or input fails closed. PostgreSQL RLS and composite foreign keys remain final tenant
authority.

## Completion and lifecycle ordering

The handler may bind a P14 run before it completes; `confirm_release` still rejects until
that exact run is `COMPLETED`. It can also bind after completion. Pause or cancellation
committed before first binding blocks the operation. A binding already committed is
historical identity and is never overwritten or represented as undone.

## Tests and scope

Real PostgreSQL coverage includes the original end-to-end failure, bounded attempt
materialization on the same campaign run, concurrent duplicate delivery, P15 ambiguous
dispatch replay, wrong workflow run, wrong workflow version, wrong schedule/occurrence,
cross-tenant service rejection, cross-tenant foreign-key rejection, pause/cancel fences,
and binding before and after P14 completion.

This corrective adds no campaign semantics to P15 or P14, starts no P17 work, performs no
deployment and does not change P25 recovery ownership.
