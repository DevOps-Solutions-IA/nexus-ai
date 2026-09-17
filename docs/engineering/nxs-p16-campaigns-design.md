# NXS-P16 Campaigns — Pre-Implementation Contract

Status: implementation active on `feat/nxs-p16-campaigns`; merge and deployment remain
unauthorized. The canonical requirement is `NXS-CAMP-001`.

## Implemented P16 contract

The implementation follows this document without changing its authority boundaries. It
uses thirteen forced-RLS tenant tables, immutable revision and sealed-audience database
guards, composite tenant foreign keys, UUIDv7 identities, PostgreSQL row locks and opaque
claim tokens. Explicit audiences are accepted in bounded sets of 500 and materialized in
transactions of at most 100 recipients with a durable snapshot cursor. Saved-segment and
import-artifact source types remain closed contracts and fail closed until their governed
resolvers exist; raw SQL and arbitrary expressions are never accepted.

Final send authorization locks the campaign run, campaign, attempt, snapshotted recipient,
customer, identity, conversation, contact preference and per-channel suppression epoch.
Suppression mutation uses that same epoch row as its serialization boundary. Campaign
pause/cancel uses the campaign row as its boundary. The transaction reserves durable
throttle capacity and commits one `AUTHORIZED` permit with stable P09 identity before any
provider I/O. Post-authorization consent or suppression changes affect future logical
sends; they do not rewrite an already authorized permit.

Final authorization denial is also durable. Consent, suppression, invalid-recipient and
quiet-hours outcomes are committed with recipient/attempt state and transition history;
only after that commit does the service return the domain error. A rollback caused by the
client-facing error therefore cannot erase the governed decision.

The runtime implements only explicit `WhatsApp`, `Email`, and `SMS` campaigns. P14 is used
for both release and recipient workflows, P15 supplies the optional release schedule, P09
is the sole message/provider path, and P04 receives bounded identifier-only lifecycle
events through the transactional outbox. Claim ambiguity remains durable and is not
automatically reassigned; P25 retains recovery authority.

## Purpose and certified authority chain

P16 decides **which immutable audience receives which campaign action and how bulk
execution is governed**. It is not a provider, workflow engine, scheduler, event broker,
compliance suite, or unbounded fan-out service.

The one canonical execution path is:

```text
P15 schedule occurrence or authorized immediate start
  -> P14 immutable campaign-release workflow
  -> P16 campaign run and bounded recipient claim
  -> P14 immutable per-recipient workflow
  -> P16 validates the completed workflow result against the campaign revision
  -> P16 final authorization transaction
  -> durable AUTHORIZED campaign_send_permit
  -> P09 MessagingService.send with the stable permit-bound idempotency key
  -> WhatsApp, Email, or SMS provider adapter
```

P15 remains the only authority for future-time campaign release. P14 remains the only
authority for workflow execution. P09 remains the only messaging transport/provider
boundary. P16 does not call provider SDKs and does not route messaging through P08/P07,
because doing so would bypass P09's certified channel normalization, conversation,
credential, delivery-state and send-idempotency controls.

P15 currently targets immutable P14 workflow versions. A scheduled campaign therefore
stores one P15 schedule targeting the campaign revision's immutable release workflow.
NXS-P16 certifies only `ONE_TIME` campaign schedules with no recurrence payload. P15's
generic recurring scheduler remains unchanged; recurring campaign series and creation of a
new immutable `CampaignRun` per recurrence require a separate future governed design.
P16 records the P15 schedule, exact dispatched occurrence and returned P14 release run.
The scheduled-release handler re-reads all three durable records and binds
`release_schedule_occurrence_id` plus `release_workflow_run_id` to the existing campaign
run under its row lock. Only a successfully completed matching release run may then
transition the campaign run to `RUNNING`. An immediate start invokes the same release
workflow directly through P14; it is not a second execution path. P16 must correlate using
durable IDs and P04 events or the trusted P15 dispatch result, then re-read authoritative
P14/P15 state rather than trusting an event payload alone.

For each eligible recipient, P16 starts the revision's immutable per-recipient P14
workflow with a stable idempotency key. A completed recipient workflow is a prerequisite
for final send authorization; it does not itself authorize delivery. P16 must then
authorize one durable tenant/attempt-bound send permit. The commit transitioning that
permit to `AUTHORIZED` is the logical-send linearization point. Only after that commit
does P16 build and submit a strict P09 `SendMessageRequest` from immutable campaign
revision fields, the snapshotted P06 identity reference, the P06 conversation boundary,
the permit-bound idempotency key, and explicitly mapped/bounded workflow outputs.
Workflow output cannot choose an account, tenant, destination, provider, URL, credential,
shell command or raw transport option.

## Dependency and ownership boundaries

`NXS-CAMP-001` depends on the certified `NXS-WF-001`, `NXS-SCHED-001`,
`NXS-WA-001`, `NXS-EMAIL-001` and `NXS-SMS-001` requirements. The P16 registry phase
continues to depend on `NXS-P09` and `NXS-P15`; P15 already depends on P14, so the phase
DAG contains the required direct channel and transitive workflow authority without a
gratuitous registry change.

P16 owns campaign drafts and immutable revisions; release/run lifecycle; immutable,
bounded audience snapshots; recipient membership and eligibility evidence;
campaign-specific consent and suppression; quiet-hour decisions; bounded materialization;
durable recipient claims and fences; organization/campaign/channel throughput limits;
per-recipient workflow and messaging identities; pause/resume/cancel fences; safe outcome
projection; append-only transition history; P04 campaign events; campaign RBAC and bounded
APIs.

P15 owns temporal occurrence calculation and scheduled release. P14 owns workflow
definition/version/run/step execution. P09 owns WhatsApp, Email and SMS accounts,
normalized destinations, credentials, provider adapters, message persistence, transport
status and outbound idempotency. P04 owns the event envelope and transactional outbox.
P06 owns customers, identities, conversations and the customer timeline.

P21 later owns the broader compliance framework: jurisdictional policy, legal-basis
taxonomy, retention, evidence export and policy administration. P23 owns billing/cost;
P24 owns the complete observability platform; P25 owns broad stale-owner recovery,
ambiguous-effect reconciliation and failover.

P16 explicitly excludes voice campaigns, predictive dialing, Asterisk/ElevenLabs,
human-agent queues/workforce routing (P17), cell and SIP scaling (P18/P19), Sentinel
(P20), the full P21 compliance suite, billing (P23), full observability (P24), automated
orphan/failover recovery (P25), DR (P26), frontend work and deployment (P32).

## Architectural invariants

- **INV-CAMP-001 — Tenant isolation.** Every campaign-owned row carries exactly one
  `organization_id`; forced RLS and tenant-aware composite foreign keys reject all
  cross-Organization campaign, customer, identity, workflow, schedule, channel account,
  audience and run references.
- **INV-CAMP-002 — PostgreSQL authority.** Campaign lifecycle, revisions, audience
  membership, materialization cursor, eligibility, suppression, consent, throttling,
  recipient claims, idempotency and terminal state are durable. Process memory, asyncio
  locks, Valkey and broker delivery never grant correctness authority.
- **INV-CAMP-003 — Immutable execution revision.** Each campaign run binds to one sealed
  campaign revision and one sealed audience snapshot. Draft edits cannot mutate running
  or historical work.
- **INV-CAMP-004 — Bounded fan-out.** Imports, audience resolution, materialization,
  claims, workflow starts, sends, retries and event production are bounded per transaction
  and per tick. There is no all-audience transaction or unbounded in-memory list.
- **INV-CAMP-005 — Consent/suppression send-permit fence.** A recipient needs affirmative
  channel consent and must pass global, campaign, customer, identity and unsubscribe
  suppression checks when one durable final send permit is authorized. The transaction
  that commits the `AUTHORIZED` permit is the linearization point for logical delivery
  authorization. Consent/suppression changes committed before it block authorization;
  changes committed afterward govern future logical sends and cannot reliably revoke
  provider-bound work that is already authorized.
- **INV-CAMP-006 — P15 temporal authority.** Future campaign release uses one P15
  schedule per campaign run/revision, never one schedule per recipient. P16 pacing uses
  durable recipient eligibility timestamps and throttle windows, not another scheduler.
- **INV-CAMP-007 — P14 workflow authority.** Release and recipient workflow work goes
  only through immutable P14 versions with stable keys. P16 cannot interpret executable
  code or call P08/P13 directly.
- **INV-CAMP-008 — P09 messaging authority.** All WhatsApp, Email and SMS delivery goes
  only through `MessagingService.send`; P16 stores no provider credential or SDK config.
- **INV-CAMP-009 — Recipient idempotency.** One campaign run/revision/recipient/channel
  identity produces at most one logical P14 recipient workflow and one logical P09 send
  identity. Physical exactly-once delivery is not claimed.
- **INV-CAMP-010 — Pause/cancel fence.** Once pause or cancellation commits, no new
  recipient claim may be authorized. Already committed downstream work remains explicit
  and is never falsely represented as undone.
- **INV-CAMP-011 — Terminal absorption.** Completed, failed and cancelled campaign runs,
  and terminal recipient executions, cannot be resurrected.
- **INV-CAMP-012 — P25 recovery boundary.** P16 records claim owner/token, attempt,
  downstream IDs and ambiguity. It does not automatically reap, reassign or reconcile
  ambiguous external effects.

## Aggregate and lifecycle model

### Campaign definition and revision

`Campaign` is the stable tenant-owned logical identity with a unique
`(organization_id, campaign_key)`. Its editable draft contains bounded descriptive data,
approved channels, release policy, audience source reference, quiet-hours/throttle policy,
and references to immutable P14 workflow versions and same-tenant P09 accounts. It does
not contain credentials or executable destinations.

`CampaignRevision` is immutable after preparation. It snapshots content/template
references, approved channel/account mapping, release and recipient workflow versions,
eligibility policy versions, quiet-hours/throttle configuration, audience source digest,
output mapping and revision hash. Active/historical runs reference a revision, never the
mutable draft.

Campaign aggregate states are:

- `DRAFT -> PREPARING | CANCELLING`
- `PREPARING -> READY | DRAFT | CANCELLING`
- `READY -> SCHEDULED | RUNNING | CANCELLING`
- `SCHEDULED -> RUNNING | CANCELLING | FAILED`
- `RUNNING -> PAUSED | COMPLETED | FAILED | CANCELLING`
- `PAUSED -> RUNNING | FAILED | CANCELLING`
- `CANCELLING -> CANCELLED`
- `COMPLETED`, `FAILED` and `CANCELLED` are absorbing.

`PREPARING -> DRAFT` records a failed validation/materialization attempt while preserving
the draft for correction. `READY` means the immutable revision and sealed audience exist;
it does not authorize delivery. `SCHEDULED` means one linked P15 schedule is active.
`RUNNING` requires the matching P14 release workflow to have completed successfully.

`CampaignRun` binds one campaign revision, audience snapshot and optional P15
schedule/occurrence. Scheduled runs use one P16-derived schedule key and one unique
schedule binding; callers cannot attach arbitrary P15 schedules. Run states include
`PENDING_RELEASE`, `MATERIALIZING`, `RUNNING`, `PAUSED`, `CANCELLING`, and absorbing
terminal states. Durable materialization and cancellation cursors make each operation
bounded and resumable. A new run needs a new logical run identity; resume never creates a
new run.

### Recipient execution

Membership and eligibility are separate from execution. Proposed recipient execution
states are:

- `PENDING -> ELIGIBLE | DEFERRED | SUPPRESSED | CANCELLED`
- `DEFERRED -> ELIGIBLE | SUPPRESSED | CANCELLED`
- `ELIGIBLE -> CLAIMED | SUPPRESSED | CANCELLED`
- `CLAIMED -> WORKFLOW_RUNNING | SUPPRESSED | FAILED`
- `WORKFLOW_RUNNING -> READY_TO_SEND | SUPPRESSED | FAILED`
- `READY_TO_SEND -> DISPATCH_AUTHORIZED | SUPPRESSED | FAILED`
- `DISPATCH_AUTHORIZED -> DISPATCHED | FAILED`
- `DISPATCHED`, `SUPPRESSED`, `FAILED` and `CANCELLED` are terminal for P16 execution.

`DISPATCH_AUTHORIZED` means the durable final send permit committed. It is an irrevocable
logical authorization for this one recipient attempt under Option 1 below; a later pause,
cancellation, unsubscribe or suppression cannot truthfully be represented as revoking the
already-authorized send. `DISPATCHED` means P09 accepted/replayed its stable logical send
and returned a message ID. It does not mean delivered, replied, converted or physically
delivered exactly once. P09 message events project later transport states without
rewriting recipient execution authority.

## Durable model

P16 implements these tenant-owned entities:

### `campaigns`

- UUIDv7 primary key, `organization_id`, unique tenant/campaign key, draft revision,
  state and optimistic `revision`.
- Mutable draft fields only; no provider secrets or raw executable fields.
- Forced RLS, tenant/key and state indexes, no runtime hard delete.

### `campaign_revisions`

- UUIDv7 primary key, tenant-aware campaign FK, unique campaign/revision number and
  canonical content hash.
- Immutable workflow-version references, P09 channel/account references, bounded content
  contract, audience-source digest, eligibility/quiet-hour/throttle policies and timestamps.
- Forced RLS; published rows cannot update/delete through the runtime role.

### `campaign_audience_snapshots`

- UUIDv7 primary key, tenant-aware revision FK, state (`OPEN`, `SEALED`, `FAILED`),
  source kind/reference/digest, source watermark, durable cursor, resolved/accepted/rejected
  counts and seal timestamp.
- Unique one active snapshot per run/revision. Sealed membership is immutable.
- Forced RLS; runtime hard delete prohibited.

### `campaign_recipients`

- UUIDv7 primary key, tenant-aware snapshot/customer/identity FKs, channel, destination
  fingerprint, membership source, eligibility state/reason, next eligibility timestamp,
  evaluated policy versions and timestamps.
- No raw provider credential. The P06 identity is re-resolved and fingerprint-matched
  before dispatch so an identity change cannot silently redirect an immutable campaign.
- Unique `(organization_id, snapshot_id, customer_id, channel)` and any additional
  identity-level uniqueness needed to collapse duplicate sources.
- Forced RLS; indexes on snapshot/cursor and eligibility/next-eligible time.

### `campaign_runs`

- UUIDv7 primary key, tenant-aware campaign/revision/snapshot FKs, optional P15
  schedule/occurrence and P14 release-run references, state/version, durable attempt and
  cancellation progress, aggregate counters, started/ended times and terminal reason.
- Unique run idempotency identity and unique tenant schedule binding.
- Forced RLS; state/time indexes; terminal rows immutable except bounded outcome
  projections derived from durable recipient/message facts.

### `campaign_recipient_attempts`

- UUIDv7 primary key, tenant-aware run/recipient FKs, attempt number, state, owner UUID,
  opaque claim token, claim/dispatch timestamps, P14 idempotency key/run ID, optional
  send-permit/message references, safe error code and correlation ID.
- Unique logical recipient execution identity and unique downstream keys. Owner/token
  compare-and-set fences every completion.
- Forced RLS; claim index by run/state/eligibility and downstream lookup indexes.
- Ambiguous attempts remain durable; automatic stale reassignment belongs to P25.

### `campaign_send_permits`

- One tenant-owned permit per recipient attempt, enforced by unique
  `(organization_id, recipient_attempt_id)` and tenant-aware FKs.
- Persisted fields include consent epoch, suppression epoch, campaign/run state version,
  authorized timestamp, opaque permit token/generation, stable P09 idempotency key, state,
  consumed/message timestamp and safe failure code.
- Closed states are `PENDING`, `AUTHORIZED`, `CONSUMED`, `FAILED` and `EXPIRED`.
  `PENDING -> AUTHORIZED | EXPIRED`; `AUTHORIZED -> CONSUMED | FAILED`. `AUTHORIZED`
  cannot be changed to `EXPIRED` by a later policy mutation because its commit is the
  logical-send linearization point. `CONSUMED`, `FAILED` and `EXPIRED` are terminal.
- There is no public permit-creation API. Only the internal repository authorization
  transaction may create/authorize it after validating current attempt owner/token,
  campaign state, recipient facts, policy epochs, quiet hours and throttle capacity.
- Forced RLS, immutable authorization facts and owner/token/state compare-and-set prevent
  cross-tenant creation, stale-worker authorization and semantic permit replacement.

### `campaign_contact_preferences` and `campaign_suppressions`

P06/P09 currently expose no consent, unsubscribe or suppression model. P16 therefore
owns the smallest campaign-safe boundary: tenant/customer/identity/channel affirmative
consent state with source/evidence reference and effective time; and tenant-global,
campaign, customer or identity suppression with stable reason/effective/expiry fields.
P16 stores references and bounded reason codes, not secret evidence bodies. P21 may later
adopt/generalize these records without weakening P16's fail-closed checks.

Consent and suppression expose separate monotonically increasing 64-bit epochs under one
tenant/customer-or-identity/channel policy row (or an equivalent normalized aggregate).
Every affirmative consent, consent revocation, unsubscribe, do-not-contact mutation,
suppression add/remove and relevant policy change locks that row and advances the affected
epoch in the same transaction as the policy mutation. Epochs never derive from timestamps
and never decrease or wrap silently. A final send permit snapshots both current epochs.
Mutations invalidate any still-`PENDING` candidate authorization by changing the epoch;
they do not retroactively revoke an already committed `AUTHORIZED` permit.

### `campaign_throttle_windows`

- Tenant/campaign-run/channel/window identity, reserved count and immutable window start.
- Unique window key; row-locked atomic reservation ensures the per-run/channel/minute
  limit and the run's durable authorized count enforces the campaign total cap.
- PostgreSQL is authoritative. Valkey may reduce contention later but cannot authorize
  sends or replace the durable counter.

### `campaign_organization_throttle_windows`

- Tenant/channel/minute identity with a unique window key and durable reserved count.
- PostgreSQL upsert plus `FOR UPDATE` locking serializes first use and contention across
  distinct campaign runs so the Organization-wide minute cap cannot oversubscribe.
- The final authorization transaction enforces the strictest of campaign-run minute,
  campaign total and Organization minute capacity before creating a permit.

### `campaign_transition_history`

- Append-only UUIDv7 row with `organization_id`, entity type/id, from/to state, stable
  reason, source, correlation ID and ordered timestamp.
- Forced RLS; no runtime update/delete; no content, destination or secret payload.

## Audience snapshot and bounded materialization

Allowed audience sources are closed and governed:

1. bounded explicit P06 customer IDs;
2. a saved, server-owned segment specification resolved through a closed field/operator
   grammar;
3. an imported ingestion artifact that already passed bounded validation, tenant binding,
   normalization and malware/content controls.

Raw SQL, arbitrary expressions, URLs, code and provider lists are never accepted. Query
segments compile only from allow-listed indexed attributes and bounded values; clients
cannot supply table/column names. Resolution records a source digest and watermark.

Preparation creates an `OPEN` snapshot, walks the source using a durable keyset cursor in
bounded chunks, inserts membership with uniqueness, records rejection reasons, and commits
each chunk. No transaction or worker holds the complete audience. A crash resumes from
the committed cursor. Only after source exhaustion, count/size ceilings and consistency
checks pass does one transaction seal the snapshot and revision. Once sealed, membership
cannot change even if the live saved segment later changes.

After the P14 release succeeds, recipient attempts are also created in bounded
transactions. The run persists its materialization cursor and completion flag atomically
with each batch; an incomplete invocation remains `MATERIALIZING` and resumes from the
committed cursor without loading or transacting over the full audience.

Default and hard batch sizes are implementation-time configuration but must be finite,
validated and covered by tests. Oversized audience requests fail before activation or
stop at an explicit tenant/campaign ceiling; they never silently spill into unbounded work.

## Eligibility, consent and suppression

Eligibility is deterministic and persisted per recipient with one stable reason code.
At minimum the evaluator checks tenant ownership, customer active state, identity active
state, destination fingerprint, certified/active P09 account, allowed WhatsApp/Email/SMS
channel, affirmative channel consent, global/campaign/customer/identity suppression,
unsubscribe/do-not-contact, duplicate membership, quiet hours, campaign revision/run
state and durable throttle eligibility.

Candidate outcomes are `ELIGIBLE`, `DEFERRED_QUIET_HOURS`, `SUPPRESSED_GLOBAL`,
`SUPPRESSED_CAMPAIGN`, `NO_CONSENT`, `UNSUBSCRIBED`, `DO_NOT_CONTACT`,
`CUSTOMER_INACTIVE`, `IDENTITY_INACTIVE`, `INVALID_DESTINATION`, `NO_CHANNEL`,
`DUPLICATE`, `TENANT_MISMATCH` and `CANCELLED`. Public APIs expose stable reasons, not
private evidence or raw destinations.

Eligibility is evaluated when the snapshot is prepared and revalidated twice at runtime:
inside the claim transaction before P14 starts, and in the final send-permit authorization
transaction after P14 succeeds. The second transaction locks the recipient attempt and
authoritative policy-epoch rows, then snapshots their current consent and suppression
epochs into the permit.

P16 chooses **Option 1: the committed `AUTHORIZED` send permit is the linearization
point**. Consent/suppression commit order is compared with durable send authorization, not
with the later physical provider call. A revocation/suppression committed first changes
the epoch and blocks permit authorization. A permit committed first may proceed to P09;
a later policy change blocks subsequent logical sends but does not pretend provider-bound
work can be recalled. This is deterministic without holding a database transaction across
provider I/O and does not overclaim real-time physical cancellation.

## Quiet hours

Quiet hours are a bounded campaign revision policy with start/end local wall time, allowed
weekdays and IANA timezone source. A trustworthy recipient timezone may be used when a
governed P06/customer preference provides it; otherwise the immutable campaign/Organization
timezone applies. Missing or invalid required timezone fails closed rather than falling
back to host time.

A recipient inside quiet hours becomes `DEFERRED` with a durable UTC `next_eligible_at`
calculated from pinned timezone rules. DST gaps move to the first valid permitted instant;
folds choose one deterministic instant. P16 workers revisit due deferred recipients in
bounded batches using PostgreSQL time. P16 does not create a P15 schedule per recipient
and does not emit one scheduler event per quiet-hour deferral.

## Throttling and backpressure

An immutable campaign revision sets bounded recipients-per-tick and channel-specific
messages-per-minute ceilings, subject to stricter Organization/global configuration. P09
provider/account limits remain an additional downstream constraint; P16 never raises a
certified P09 limit.

Initial claim order is campaign run then recipient; it grants bounded P14 workflow
authority but does not consume the later P09 send window while that workflow runs. Final
send-permit order is campaign run, recipient attempt, consent/policy rows, suppression rows
and both run/Organization throttle-window rows in one documented deterministic sequence.
Race-safe PostgreSQL upserts establish missing window rows before locking them. That
transaction checks
run `RUNNING`, current owner/token, current policy epochs, recipient validity, database
time, quiet hours and remaining durable window capacity, atomically reserves one send unit,
and authorizes the permit. `FOR UPDATE SKIP LOCKED` distributes initial work between
replicas. No asyncio semaphore grants authority. Exhausted capacity leaves the recipient
`READY_TO_SEND` for a later bounded authorization tick. Retry loops and provider rate-limit
handling have explicit attempt/time ceilings and cannot become hidden storms.

## Recipient idempotency and dispatch fencing

The semantic recipient identity is the canonical tuple:

```text
organization_id + campaign_id + campaign_revision + campaign_run_id
+ audience_recipient_id + channel
```

P16 persists a bounded SHA-256-derived canonical identity before any downstream call.
The P14 key is `campaign:<identity>:workflow`; the P09 key is
`campaign:<identity>:message` (or equivalent bounded encodings). The P09 key is persisted
on the send permit before authorization commits. Both remain unchanged across worker
restart, retry and replay. Same key/different semantic fingerprint is a conflict, never a
silent second action.

Dispatch sequence:

1. lock run and eligible recipient; re-check lifecycle and pre-workflow eligibility, then
   persist owner/token plus the stable P14 key;
2. commit the claim;
3. call P14 `start_run` for the immutable per-recipient workflow using the P14 key;
4. reacquire and verify state/owner/token, then persist the P14 run ID;
5. observe/re-read terminal P14 success and validate only the revision's closed output map;
6. in one final authorization transaction, lock the campaign run and recipient attempt,
   lock/read current consent and suppression epochs, revalidate campaign `RUNNING`, owner,
   recipient, quiet hours and throttle capacity, persist the stable P09 key, reserve the
   send window, and commit one `AUTHORIZED` send permit with its epoch snapshots and token;
7. after commit and without holding database locks, call P09 `MessagingService.send` with
   trusted tenant context, same-tenant account and conversation, revision-defined content,
   snapshotted identity and the permit's stable P09 key;
8. reacquire and compare permit/attempt state, owner and token; persist message ID, move
   permit to `CONSUMED`, and mark the attempt `DISPATCHED`.

P14 failure blocks P09. P09's own idempotency/fingerprint contract remains final authority
for the external send. If P09 accepts but P16 loses its terminal write, replay uses the
same P09 key and resolves the same logical message or an honest ambiguous/failed result;
P16 must not invent a new key. Multiple workers replaying the same authorized permit can
only present the same logical request/key to P09; they cannot authorize another permit.
Broad ambiguous-effect reconciliation is P25.

### Send-permit race semantics

- **Unsubscribe/suppression before permit authorization:** the policy mutation locks and
  increments its epoch first. The authorization transaction observes non-consent or
  suppression/current epoch and cannot commit `AUTHORIZED`; P09 is not called.
- **Permit authorization before unsubscribe/suppression:** the `AUTHORIZED` commit wins
  logical-send ordering. P09 may be called even if policy changes before provider I/O.
  The new epoch blocks every future logical recipient execution; it cannot revoke this one.
- **Campaign pause/cancel before permit authorization:** the authorization transaction
  sees a non-`RUNNING` campaign and blocks. If the permit commits first, it may proceed;
  pause/cancel fences all later permits but does not falsify this authorization.
- **Permit/P09 ambiguity:** an authorized permit remains bound to one stable P09 key. P09
  unavailability may produce a bounded same-permit retry when P09 proves no acceptance;
  accepted-but-unrecorded outcomes remain durable ambiguity for same-key replay/P25, never
  a new permit or key.

## P15 release integration

A scheduled campaign request contains one governed future-time specification, not recurrence
or a caller-controlled schedule ID. P16 rejects `RECURRING` and any recurrence payload before
creating a campaign run or P15 schedule. P16 derives the stable schedule key, creates or
reuses exactly one same-tenant `ONE_TIME` P15 schedule for the campaign run, targeting the
immutable P14 release workflow and carrying only bounded campaign/run/revision identifiers.
P16 stores the P15 schedule ID under tenant-aware uniqueness and validates the complete
immutable binding before reuse.

After P15 dispatches an occurrence, the P16-owned scheduled-release handler accepts the
exact occurrence/run identities from that trusted result, then re-reads the tenant-scoped
P15 occurrence, P15 schedule and P14 workflow run. It verifies Organization, campaign,
campaign revision, campaign run, schedule, occurrence, release workflow version, workflow
run and the closed schedule/workflow input. One transaction then locks the campaign and run
and writes both `release_schedule_occurrence_id` and `release_workflow_run_id`. Tenant-aware
foreign keys and unique occurrence binding protect the durable relationship. A replay with
the same pair is idempotent; any different occurrence or workflow run is fenced and cannot
overwrite it. No fuzzy correlation, recent-run search or mutable event payload is authority.

Binding may occur while the P14 workflow is still running, but `confirm_release` remains
blocked until that exact run is `COMPLETED`. Binding after completion is equally valid.
Campaign pause or cancellation committed before the first binding prevents it. A same-ID
delivery replay after a successful binding remains idempotent even if later lifecycle state
has advanced.

Duplicate P15 wakes resolve through the stable release-run identity and campaign transition
compare-and-set. Cancelling the P15 schedule prevents future wake-up but does not itself
rewrite an already-running campaign. Cancelling P16 cancels its still-pending P15 schedule
explicitly when possible and records each independent result. Recipient pacing never
creates P15 schedules.

## Pause, resume and cancellation

- **Pause:** `RUNNING -> PAUSED` under the run-row lock. After commit, no new recipient
  claim or send-permit authorization can succeed. A claim committed first remains explicit
  but cannot reach P09 without a permit; a permit committed first may proceed under its
  existing owner/token and stable P09 key.
- **Resume:** only `PAUSED -> RUNNING`; continue from durable snapshot/materialization and
  recipient states. Already dispatched identities never return to claimable state.
- **Cancel:** `DRAFT`, `PREPARING`, `READY`, `SCHEDULED`, `RUNNING` or `PAUSED` first moves
  to durable `CANCELLING`. Pending/claimed/workflow/ready attempts and eligible/deferred
  recipients become `CANCELLED` in bounded, replay-safe batches. Run-owned counters record
  committed progress; the run/campaign becomes terminal `CANCELLED` only when no remaining
  cancellable attempt exists. No new claim or permit authorization succeeds after the
  initial cancellation commit.
- **Committed work:** P14 runs and P09 messages accepted before cancellation are preserved
  as issued. P16 may call an existing explicit P14 cancellation boundary for a known
  in-flight run only when authorized and must report its independent outcome. There is no
  representation of message recall unless P09 later certifies one.

Pause/resume/cancel races serialize on campaign run before recipient/permit. Commit order
against `AUTHORIZED` permit creation is the contract; terminal states absorb and stale
owner/token writes fail.

## Outcome model

P16 derives bounded aggregate counters from durable rows and rebuildable projections:

- audience: planned, materialized, duplicate, invalid;
- eligibility: eligible, deferred, suppressed, no-consent, unsubscribed;
- execution: claimed, workflow-running, ready-to-send, dispatched, failed, cancelled;
- transport projection from P09: queued/sent or accepted, delivered, failed, bounced when
  the certified channel reports it, replied and unsubscribed when existing events support it.

Campaign execution outcome is separate from P09 transport outcome and from business
conversion. `COMPLETED` means every materialized recipient is terminal for P16; it does
not mean every provider delivered or that the campaign caused a conversion. P09 remains
source of truth for message transport status. Business attribution requires a separately
governed model and is never inferred from a send count.

## API proposal

The implementation may refine paths without changing ownership:

- `POST /api/v1/campaigns`
- `GET /api/v1/campaigns`
- `GET /api/v1/campaigns/{campaign_id}`
- `PATCH /api/v1/campaigns/{campaign_id}` with expected revision
- `POST /api/v1/campaigns/{campaign_id}/prepare`
- `POST /api/v1/campaigns/{campaign_id}/schedule`
- `POST /api/v1/campaigns/{campaign_id}/start`
- `POST /api/v1/campaigns/{campaign_id}/pause`
- `POST /api/v1/campaigns/{campaign_id}/resume`
- `POST /api/v1/campaigns/{campaign_id}/cancel`
- `GET /api/v1/campaigns/{campaign_id}/audience`
- `GET /api/v1/campaigns/{campaign_id}/recipients`
- `GET /api/v1/campaigns/{campaign_id}/runs`
- `GET /api/v1/campaigns/{campaign_id}/transitions`

All request models use `extra="forbid"`, bounded strings/collections/pagination and RFC
9457 NXS errors. Organization authority comes only from trusted auth context. There is no
authoritative request-body `organization_id`, raw SQL, arbitrary target/URL/provider or
credential field.

## RBAC proposal

Use the existing resource/action catalog and deny-by-default authorization service:

- `campaign:read`: view campaign definitions, revisions, bounded audience/recipient
  status, outcomes and transitions;
- `campaign:execute`: prepare, schedule, start, pause, resume and cancel;
- `campaign:configure`: create and revision-edit campaigns, content and bounded policies;
- `campaign:audience`: optional separate permission for importing/selecting audience
  membership, only if implementation proves separation reduces privilege. It must not be
  implied by read.

Owners/admins receive deliberate grants through the P16 migration; member grants require
an explicit product decision and default to no campaign execution/configuration. Existing
P09/P14/P15 permissions are not transitively granted to campaign callers; the internal P16
service principal holds only the narrow operations required by the canonical path.

## P04 events

Candidate transactional events are `campaign.created`, `campaign.prepared`,
`campaign.scheduled`, `campaign.started`, `campaign.paused`, `campaign.resumed`,
`campaign.cancelled`, `campaign.completed`, `campaign.failed`,
`campaign.recipient.eligible`, `campaign.recipient.suppressed`,
`campaign.recipient.claimed`, `campaign.recipient.workflow_started`,
`campaign.recipient.dispatched` and `campaign.recipient.failed`.

Events contain tenant/campaign/revision/run/recipient opaque IDs, channel, state, stable
reason, counts and correlation/causation IDs. They never contain the audience, destination,
message body, subject, customer profile, credentials, provider authorization, workflow
input/output or hidden reasoning. High-volume recipient events use existing bounded outbox
backpressure and idempotent consumers; broker delivery never grants recipient authority.

## Failure matrix

| # | Scenario | Authoritative state / allowed result | Forbidden result | Durable protection / owner |
|---:|---|---|---|---|
| 1 | Invalid audience contract | Draft remains `DRAFT`; bounded validation error | Partial live fan-out | Closed source grammar and limits |
| 2 | Cross-tenant recipient | Reject/not found | Membership or dispatch | RLS and composite FKs |
| 3 | Invalid channel | Reject revision | Provider fallback | Closed WhatsApp/Email/SMS enum |
| 4 | Missing consent | Recipient `SUPPRESSED/NO_CONSENT` | Workflow/send | Persisted affirmative-consent check |
| 5 | Suppressed recipient | Stable suppression reason | Workflow/send | Campaign suppression boundary |
| 6 | Quiet hours | `DEFERRED` with bounded next eligibility | Immediate send | IANA policy and DB timestamp |
| 7 | Duplicate recipient source | One membership, duplicate count | Two executions | Snapshot uniqueness |
| 8 | P09 account unavailable | Block before send or stable failure | Different account/provider fallback | Immutable same-tenant account reference |
| 9 | P14 rejects workflow start | Recipient fails/classifies safely | P09 send | P14-success-before-P09 rule |
| 10 | Duplicate P15 wake | Existing release identity/run returned | Second campaign run | Stable keys and run CAS |
| 11 | Pause versus claim | Commit order decides | Claim after committed pause | Run-first row lock |
| 12 | Cancel versus claim | Commit order decides; pending work cancelled | Claim after committed cancel | Run-first row lock and state predicate |
| 13 | Two workers claim recipient | One owner/token | Two downstream authorities | `SKIP LOCKED`, CAS and uniqueness |
| 14 | P09 accepted, P16 terminal write lost | Same-key replay or durable ambiguity | New send key | P09 idempotency; P25 reconciliation |
| 15 | Duplicate downstream acknowledgement | Idempotent projection | Counter double increment | Message/event receipt uniqueness |
| 16 | Oversized audience | Reject/stop at explicit ceiling | Unlimited rows/memory | Source and tenant campaign caps |
| 17 | Materialization crash | Resume committed keyset cursor | Restart from zero/duplicate membership | Durable cursor and uniqueness |
| 18 | Resume after process crash | Reconstruct non-ambiguous durable work | Trust local queue | PostgreSQL authority |
| 19 | Destination becomes invalid | Suppress/fail before P09 | Redirect silently | Identity fingerprint re-check |
| 20 | Unsubscribe during campaign before authorization | Epoch advances; permit authorization fails | New logical send authority | Locked policy epoch and permit transaction |
| 21 | Channel rate limit | Defer/bounded classified retry | Tight retry loop | Durable throttle/backoff ceiling |
| 22 | Partial campaign completion | Explicit counters and remaining rows | Mark all delivered | Durable recipient states |
| 23 | Database contention | Skip locked/bounded retry | Unbounded lock wait | Batches, lock order and timeout |
| 24 | Stale claim completion | Reject owner/token mismatch | Overwrite current state | CAS fence; recovery P25 |
| 25 | Outbox unavailable | Business row/outbox commit together; broker retry | State without event row | P04 transactional outbox |
| 26 | Bad campaign revision/hash | Reject prepare/start | Execute mutable draft | Immutable revision/hash validation |
| 27 | Expired release window | Cancel/fail with stable reason | Late unbounded start | P15 occurrence and campaign policy |
| 28 | P15 schedule cancelled | Campaign stays/cancels explicitly | Hidden immediate fallback | Stored schedule state and event re-read |
| 29 | P09 rejects message | Recipient stable failure | Provider bypass | P09 error taxonomy only |
| 30 | Downstream timeout | Honest ambiguous/failed state, same key | Blind new-key retry | P14/P09 idempotency and P25 boundary |
| 31 | Tenant disabled | Stop claims and fail closed | Continue fan-out | Trusted organization lifecycle check |
| 32 | Saved segment changes after seal | Existing snapshot unchanged | Live membership mutation | Snapshot digest/seal |
| 33 | Import contains malicious formula/code | Reject or treat as bounded data | Execute/import code | Governed ingestion and no eval |
| 34 | Release workflow fails | Campaign `FAILED` or stays non-running | Recipient materialization/dispatch | P14 release run authority |
| 35 | Recipient workflow succeeds with invalid output | Stable recipient failure | Unvalidated P09 request | Closed revision output mapping |
| 36 | Provider reports bounce/delivery out of order | P09-normalized projection rules | Rewrite dispatch authority | P09 status source and idempotent consumer |
| 37 | Unsubscribe commits before permit | No `AUTHORIZED` permit; recipient suppressed | P09 call | Monotonic consent epoch under final transaction |
| 38 | Unsubscribe commits after permit before P09 call | Existing permit may proceed; future identities blocked | Pretend existing permit was revoked | Option 1 authorization linearization |
| 39 | Suppression commits before permit | No `AUTHORIZED` permit | P09 call or stale epoch acceptance | Monotonic suppression epoch and locked re-check |
| 40 | Campaign cancel after workflow success before permit | Attempt cannot authorize; campaign/recipient cancel | P09 call | Run lock/state predicate in permit transaction |
| 41 | Permit authorized but P09 unavailable | Bounded same-permit/same-key outcome or failure | New permit/key or provider bypass | Durable permit and P09 error/idempotency contract |
| 42 | Permit authorized, P09 accepts, terminal write lost | Permit remains authorized/ambiguous; same-key replay | Second logical message | Permit/P09 identity; broad reconciliation P25 |
| 43 | Expired pending permit replay | Reject; re-evaluate under current epochs through governed authorization | Promote stale pending permit | State absorption, epoch comparison and unique attempt |

## Concurrency matrix

| # | Race | PostgreSQL serialization/fencing | Deterministic result |
|---:|---|---|---|
| 1 | Create same campaign key | Unique tenant/key plus request fingerprint | One campaign; replay or conflict |
| 2 | Publish/update same draft | Row lock and expected revision | One immutable revision wins |
| 3 | Build same audience snapshot | Unique active snapshot/revision plus cursor lock | One membership set |
| 4 | Two materialization workers | Snapshot cursor `FOR UPDATE`; recipient uniqueness | Disjoint bounded chunks, no duplicates |
| 5 | Claim same recipient | Run-first lock, `SKIP LOCKED`, state CAS, owner/token | One execution authority |
| 6 | Pause versus claim | Lock run before recipient | Pause winner blocks; prior claim explicit |
| 7 | Cancel versus claim | Lock run before recipient | Cancel winner blocks; prior claim explicit |
| 8 | Resume versus claim | State/version CAS under run lock | One resume, then bounded claims |
| 9 | Duplicate P14 start | Persisted recipient workflow key | One logical workflow run |
| 10 | P09 accepted versus lost local write | Persisted send key/message fingerprint | Replay same logical message; ambiguity retained |
| 11 | Suppression update versus claim | Preference/suppression check in claim transaction | Suppression winner blocks authorization |
| 12 | Unsubscribe versus final send permit | Policy row and attempt locked in deterministic order | Unsubscribe-first blocks authorization; permit-first may proceed and blocks only future sends |
| 13 | Throttle reservation contention | Unique window row and atomic reserved-count predicate | Capacity cannot be oversubscribed |
| 14 | P15 wake versus campaign cancel | Campaign run state/version CAS | Exactly one release/cancel ordering wins |
| 15 | Suppression versus final send permit | Suppression epoch and attempt locked in permit transaction | Suppression-first blocks; permit-first remains authorized |
| 16 | Campaign cancel versus final send permit | Run locked before attempt/policy/throttle rows | Cancel-first blocks; permit-first may proceed while later permits stop |
| 17 | Authorized permit versus P09 acceptance/lost terminal write | One permit/attempt and stable unique P09 key | Same logical P09 replay; no second permit or message identity |
| 18 | Duplicate scheduled-release result | Campaign/run row locks plus immutable occurrence/run pair | Same pair replays successfully; one binding transition |
| 19 | Different P14 run versus existing release binding | Compare incoming run with P15 occurrence and persisted run ID | Mismatch is fenced; existing binding is never overwritten |
| 20 | Campaign pause/cancel versus release binding | Campaign row locks before campaign-run binding | Lifecycle winner blocks the first binding; prior exact binding remains immutable |

No correctness rule depends on an asyncio semaphore, process-local cursor, singleton
worker, Valkey lock or event arrival order.

## Security and abuse red-team answers

1. **Can a caller submit arbitrary recipients without a bound?** No; every source and
   materialization path has tenant/campaign/item/batch ceilings.
2. **Can a caller submit raw SQL segmentation?** No; only explicit IDs, governed imports
   or a closed saved-segment grammar are valid.
3. **Can a campaign execute a URL, shell, SQL or Python?** No; none is a campaign target
   or field, and P14 accepts only its certified closed step types.
4. **Can a campaign store provider credentials?** No; it references same-tenant P09
   accounts, while P09/Vault retain credentials.
5. **Can P16 call a provider SDK directly?** No; P09 `MessagingService.send` is the sole
   channel boundary.
6. **Can P16 bypass P14?** No recipient send permit exists until the stable P14 workflow
   reaches successful completion.
7. **Can P16 route messaging through arbitrary P08/P07 tools?** No; campaign delivery is
   P09-only, preventing duplicate messaging policy/provider paths.
8. **Can tenant A select tenant B's customer, workflow, schedule or channel account?** No;
   trusted context, forced RLS and composite FKs fail closed.
9. **Can a request-body `organization_id` switch scope?** No; no authoritative body field
   exists and the service uses trusted auth context.
10. **Can missing consent be treated as consent?** No; affirmative channel consent is
    required and unknown/missing states suppress.
11. **Can suppression be bypassed after audience preparation?** No; it is rechecked before
    P14 authorization and in the durable final send-permit transaction.
12. **Can two replicas send the same recipient twice?** They may contend, but one durable
    owner/token and stable P14/P09 keys authorize one logical execution.
13. **Can a pause/cancelled campaign continue claiming?** No; run-first locks and state
    predicates fence all new claims after commit.
14. **Can a quiet-hours recipient be sent immediately?** No; it is durably deferred until
    a deterministic permitted UTC instant.
15. **Can a one-day outage release an unlimited backlog?** No; audience batches, throttle
    windows, attempts and catch-up/wake behavior are bounded.
16. **Can campaign edits change an active run?** No; the run references an immutable
    revision and sealed audience snapshot.
17. **Can workflow output redirect the recipient or provider?** No; destination/account
    come from trusted revision/snapshot references and output uses a closed bounded map.
18. **Can retries create another logical provider send?** No; the P09 key remains stable;
    ambiguous effects are recorded rather than retried with a new key.
19. **Can recipient/customer payloads leak through events?** No; events contain opaque IDs,
    state and stable reason/count fields only.
20. **Can campaign credentials or audience data reach an LLM?** No credentials are stored
    in P16, and P14/P13 receive only bounded workflow input explicitly approved by the
    immutable revision; raw audience/customer profiles are prohibited.
21. **Can unsubscribe race after workflow completion?** Yes; the final durable permit
    transaction is the deterministic decision point. Unsubscribe-first blocks it.
22. **Can consent change after permit commit?** Yes; it advances the epoch and blocks future
    logical sends, but cannot retroactively falsify the already-authorized logical send.
23. **Can a worker forge a permit?** No; a permit is tenant/attempt-bound, unique, linked to
    the current owner/token and policy epochs, and created only by governed repository logic.
24. **Can a stale permit be reused for another logical send?** No; unique attempt identity,
    closed permit transitions and the stable P09 key prevent another logical message.

## Implementation certification criteria

The certification claim is **CAMPAIGN CONTRACT + DURABILITY + CONSENT FENCING
CERTIFIED** for WhatsApp, Email and SMS. It is not physical exactly-once provider
delivery, voice-campaign certification, global compliance certification, conversion
attribution, broad failover/orphan recovery, or deployment certification.

The feature branch has completed the NXS lifecycle but is not canonical until its PR is
independently reviewed and merged. Corrective implementation after the P15 rebase must
reprove real PostgreSQL tenant isolation, bounded materialization/cancellation, durable
denial outcomes, consent/suppression races, all three throttle scopes, P16-owned P15
binding, exact P15-occurrence/P14-release-run bridging, P14/P09 idempotency, complete
regression gates and exact-head CI/security.
