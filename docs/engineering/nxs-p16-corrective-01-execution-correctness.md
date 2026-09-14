# NXS-P16 Corrective 01 — Campaign execution correctness

Status: corrective implementation on `feat/nxs-p16-campaigns`; merge, P17 and deployment
remain unauthorized.

## Canonical continuity

The P16 branch was rebased without conflicts from its prior closure head
`3bc0bc943a158d50545d1d1db7f4d5343079c3d3` onto canonical main
`d2b7f487adc1c3593050b856ef59cdfe7819b18b`. This preserves the merged P15 bounded
misfire-accounting corrective and the P16 campaign implementation/governance lineage.
P15 `SKIP`, `FIRE_ONCE`, `CATCH_UP_BOUNDED`, coalesced accounting, atomic cursor and
revision-safe identities are unchanged.

## Fail-first finding and durable denial

The fail-first PostgreSQL regression test called final authorization after an unsubscribe.
The service raised the expected domain error, but a new transaction observed the attempt
still in `READY_TO_SEND`; the prior implementation had persisted `SUPPRESSED` and then
raised inside the same transaction, causing rollback.

Final authorization now returns its denial decision from repository code after persisting
recipient state/reason, attempt state/error, policy epochs and transition history. The
transaction commits, and only then does the service raise the client-facing domain error.
Quiet-hours denial remains retryable in `READY_TO_SEND` while persisting
`DEFERRED_QUIET_HOURS` and `next_eligible_at`; non-retryable consent/suppression decisions
are terminal `SUPPRESSED`. Cancellation commits `CANCELLING` before any later authorization
can proceed and terminalizes attempts in bounded transactions.

## P16-owned scheduling

The schedule API accepts a closed P15 temporal specification instead of `schedule_id`.
P16 creates a scheduled campaign run, derives
`campaign.<campaign_uuid>.r<revision>`, and creates or reuses one P15 schedule targeting
the immutable release workflow with the campaign, revision and run IDs. Reuse requires an
exact semantic match. Tenant-aware foreign keys and unique run/schedule binding reject
cross-tenant, arbitrary and cross-purpose schedules; recipient-level schedules are not an
API or service capability.

## Authoritative throttling

The final permit transaction enforces all immutable policy limits:

1. campaign-run/channel/minute using `campaign_throttle_windows`;
2. campaign total using the run's locked `authorized_count`;
3. Organization/channel/minute using `campaign_organization_throttle_windows`.

Window creation uses PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` followed by
`FOR UPDATE`, including the first-use race. Capacity is incremented only after every
applicable limit passes, in the same transaction that authorizes the permit. Process
memory, asyncio locks and Valkey grant no authority.

## Bounded processing

After release-workflow success a run transitions to `MATERIALIZING`. Each invocation
creates at most the configured attempt batch, advances a durable cursor in the same
transaction, and moves to `RUNNING` only after the sealed snapshot is exhausted. Rollback
advances neither attempts nor cursor; replay resumes from committed progress.

Cancellation first moves the campaign and nonterminal runs to `CANCELLING`, fencing new
claims and permits. Each invocation locks at most one configured batch with
`SKIP LOCKED`, terminalizes attempts/recipients, and increments durable per-run progress.
Only a pass observing no remaining cancellable attempts may move runs and campaign to
absorbing `CANCELLED`. Repeated processors are idempotent and crash-resumable.

## Preserved boundaries

The send sequence remains P14 success, committed P16 `AUTHORIZED` permit, then P09 send.
The permit commit remains the logical-send linearization point. No P15 runtime behavior,
P17 functionality, provider bypass, physical exactly-once claim, orphan reassignment or
deployment is introduced. Broad ambiguous-effect recovery remains P25.
