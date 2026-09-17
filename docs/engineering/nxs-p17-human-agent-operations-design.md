# NXS-P17 Human Agent Operations — Pre-Implementation Contract

## Status and purpose

This document is the governance contract for NXS-P17. It defines the durable authority,
ownership, security, concurrency and recovery boundaries that implementation must satisfy.
It does not activate NXS-P17, create a phase manifest, authorize migrations, or certify any
runtime capability.

NXS-P17 answers: **which authorized human may act on an interaction now?** It does not own
provider transport, media, model execution, generic workflows, campaign scheduling or
infrastructure.

## Dependency decision

The phase registry depends directly on `NXS-P09` and `NXS-P13`.

- P09 brings the certified messaging boundary and transitively preserves P06 customer and
  conversation identity plus P04/P03 platform dependencies.
- P13 brings the certified AI runtime and transitively preserves P12 voice and P11 telephony.
- P18 and later phases are not dependencies.

`NXS-HUMAN-001` records the exact requirement-level authorities:

| Requirement | Decision | Reason |
|---|---|---|
| `NXS-AUTH-006` | Required | Organization-scoped RBAC and deny-by-default permission checks. |
| `NXS-EVENT-003` | Required | Authority mutations and P04 event intent must commit atomically. |
| `NXS-CUSTOMER-001` | Required | Canonical customer, conversation and channel identity. |
| `NXS-WA-001` | Required | Certified WhatsApp conversation and P09 delivery boundary. |
| `NXS-EMAIL-001` | Required | Certified Email conversation and P09 delivery boundary. |
| `NXS-SMS-001` | Required | Certified SMS conversation and P09 delivery boundary. |
| `NXS-TEL-001` | Transitive | Required through `NXS-VOICE-001`; P17 does not manipulate telephony transport. |
| `NXS-VOICE-001` | Required | Certified call/session references and AI-human voice handoff boundary. |
| `NXS-AGENT-001` | Required | The only copilot and return-to-AI model execution boundary. |
| `NXS-WF-001` | Not required | P17 does not implement or require generic workflow orchestration. |

## Ownership boundary

P17 owns:

- human-agent work queues and bounded routing policy;
- explicit agent presence, capacity and active assignment accounting;
- durable work items, assignments, ownership generations and claim tokens;
- single-mode conversation ownership (`AI`, `HUMAN`, `UNASSIGNED`);
- controlled AI-to-human handoff and human-to-AI return;
- human-to-queue and human-to-agent transfer;
- supervisor inspection, release, requeue and transfer;
- human action authorization and audit history;
- advisory copilot context references;
- queue SLA timestamps and bounded operational views;
- P04 human-operations events and P17 RBAC.

P17 references but does not own:

- P06 customer and conversation identity;
- P09 messaging transport and provider adapters;
- P11/P12 call, voice-session and media authority;
- P13 model execution and agent runtime;
- P04 outbox and event transport.

## Architectural invariants

### INV-HUMAN-001 — Tenant isolation

Every P17 row belongs to exactly one `organization_id`. Forced RLS, trusted tenant context
and tenant-aware composite foreign keys enforce same-Organization queues, users,
conversations, customers, calls, voice sessions, work items, assignments and actions.

### INV-HUMAN-002 — PostgreSQL authority

PostgreSQL is authoritative for queue membership, presence, capacity, assignments, claims,
lease versions, ownership, handoff, action authorization, supervisor mutations and terminal
state. WebSocket connectivity, browser state, Valkey and process-local locks are never
authority.

### INV-HUMAN-003 — Exclusive conversation authority

A conversation has one durable ownership mode and monotonically increasing ownership
generation. `AI` and `HUMAN` cannot be simultaneously authoritative. Every customer-facing
action verifies the current generation. `AI` mode is valid only when the exact accepted P13
execution identity is durably bound to that tenant, conversation and generation; requesting
an AI return does not grant AI authority.

### INV-HUMAN-004 — Fenced human ownership

An assignment has an opaque claim token and monotonically increasing lease version. A
stale token/version cannot accept, reply, transfer, complete, requeue or return control.

### INV-HUMAN-005 — Explicit presence

Only an explicit, versioned presence mutation changes operational availability. A connected
browser or WebSocket never implies `AVAILABLE`.

### INV-HUMAN-006 — Bounded capacity

Claim authorization serializes the agent presence/capacity row and must prove that active
assignments remain below the configured bound after the claim commits.

### INV-HUMAN-007 — Certified external boundaries

Human messaging actions use P09. Voice handoff stores P11/P12 identifiers but never controls
SIP or media. Copilot and AI return use P13. No direct provider, model or arbitrary network
path is permitted.

### INV-HUMAN-008 — Advisory copilot

Copilot output cannot send, execute tools, transfer ownership, modify a CRM, place a call or
authorize a campaign. An authorized human must explicitly approve every externally visible
action through a governed P17 action authorization.

### INV-HUMAN-009 — Durable audit

Every authority-changing operation appends immutable transition history and transactional
P04 event intent with bounded identifiers, reason codes and correlation IDs.

### INV-HUMAN-010 — Terminal absorption

Completed and cancelled work items and terminal assignments cannot be resurrected. New work
requires a new work item and identity.

### INV-HUMAN-011 — Deterministic routing

Eligible work orders by `priority DESC`, `eligible_at ASC`, `created_at ASC`, `id ASC` after
closed, bounded queue/channel/skill filters. No arbitrary SQL, Python or opaque ML routing.

### INV-HUMAN-012 — Recovery boundary

P17 persists the evidence needed to detect stale or ambiguous ownership. It supports explicit
authorized release, transfer and requeue. P25 owns automatic lease reaping, orphan
reassignment, ambiguous downstream reconciliation and failover.

### INV-HUMAN-013 — Global lock order

Every P17 authority transaction acquires only the rows it needs, but always in this order:
queues, agent-presence rows, work item, conversation ownership, assignments, handoff record,
then action authorization. Multiple queues, agents or assignments are locked by ascending UUID
or stable identifier. No transaction may acquire an earlier class after a later class.

## Durable model proposal

This is a design proposal only; no P17 tables are created by governance alignment.

### `human_queues`

- PK: UUID `id`; tenant key: unique `(organization_id, id)`.
- Unique stable identity: `(organization_id, queue_key)`.
- Mutable: bounded name, enabled state, supported closed channel set, priority policy,
  required skill/tag set, maximum active assignments per agent, SLA targets and revision.
- No arbitrary executable routing expressions.
- Forced RLS; restrictive deletion once referenced. Disable instead of destructive deletion.

### `human_agent_presence`

- PK/unique: `(organization_id, agent_user_id)`.
- State: `OFFLINE`, `AVAILABLE`, `BUSY`, `AWAY`, `WRAP_UP`.
- Fields: capacity, active assignment count, version and `updated_at`.
- Presence and capacity changes use compare-and-set on `version`.
- The active count is transactionally maintained and reconcilable from assignment rows.

### `human_work_items`

- PK: UUID `id`; tenant key: unique `(organization_id, id)`.
- Required references: `conversation_id`, optional `customer_id`, `queue_id`, channel,
  `source_type`, `source_id`, handoff reason and correlation ID.
- Scheduling/routing fields: priority, `eligible_at`, `queued_at`, required skills/tags.
- Ownership fields: current assignment ID, assigned agent ID, claim token, lease version,
  `claimed_at`, `accepted_at`, `first_response_at`, `completed_at`.
- State and reason fields are closed enums/codes. Conversation content is not duplicated.
- Duplicate handoff uniqueness is based on same tenant, source identity and active handoff
  generation.

### `human_assignments`

- Immutable assignment identity for each claim or transfer generation.
- Fields: work item, owner agent, queue, opaque claim token digest, lease version,
  acquired/accepted/released/completed timestamps and release reason.
- Only one non-terminal assignment per work item; token values are never reused.
- Assignment history remains after transfer or completion.

### `conversation_ownership`

- Unique `(organization_id, conversation_id)`.
- Mode: `AI`, `HUMAN`, `UNASSIGNED`; optional current assignment/agent/AI-run reference.
- Monotonic `ownership_generation` is incremented for every authority change.
- `AI` requires a same-tenant P13 execution identity and stable P13 idempotency identity bound
  to the resulting ownership generation. `UNASSIGNED` carries no implied AI authority.
- A check constraint requires fields appropriate to the selected mode.

### `human_handoffs`

- Durable tenant/conversation/work-item record for AI-to-human and human-to-AI transitions.
- Human-to-AI state is closed: `PENDING_P13_ACCEPTANCE`, `P13_ACCEPTED`, `P13_REJECTED` or
  `P13_ACCEPTANCE_AMBIGUOUS`.
- Binds the Organization, conversation, pending ownership generation, stable P13 idempotency
  identity, expected P13 execution contract/version and, only after acceptance, the exact P13
  execution/run identity and resulting AI ownership generation.
- Unique semantic return identity makes duplicate delivery idempotent. A different P13 run for
  an already-bound return is a conflict and cannot overwrite the binding.
- Known P13 rejection is a P17-owned deterministic outcome. Ambiguous acceptance remains
  `UNASSIGNED` and is not auto-requeued or retried with another identity.

### `human_action_authorizations`

- One durable authorization per semantic customer-facing human action.
- Binds tenant, conversation, assignment, claim token version, ownership generation, channel,
  stable downstream idempotency key and bounded action fingerprint.
- Commit of `AUTHORIZED` is the logical action-authorization linearization point. A transfer,
  supervisor release or AI return committed first blocks authorization. A previously
  authorized action may proceed and cannot honestly be represented as revoked after provider
  handoff.
- Accepted-but-not-persisted ambiguity remains recorded and is not retried with a new key.

### `human_transition_history`

- Append-only tenant-owned audit rows for work items, assignments, ownership and presence.
- Captures actor user, source, action, entity, prior/new state, reason, correlation and time.
- No message body, credentials, raw customer profile or hidden reasoning.

## Queue and presence contract

Queues have a stable key, closed supported-channel set, bounded skill/tag filters, per-agent
capacity ceiling and SLA metadata. Disabled queues cannot receive new work or claims; existing
work remains durably visible for explicit requeue/cancel decisions.

Presence is separate from authentication and socket connectivity:

| State | May receive new work | Meaning |
|---|---|---|
| `OFFLINE` | No | Agent explicitly ended operational availability. |
| `AVAILABLE` | Yes, below capacity | Agent is eligible for governed claims. |
| `BUSY` | Policy-dependent, below capacity | Agent owns work; additional claims require queue policy. |
| `AWAY` | No | Agent is temporarily unavailable. |
| `WRAP_UP` | No | Agent is completing post-interaction work. |

An agent cannot set capacity above repository-configured bounds. Queue limits may be stricter.

## Work-item state machine

States:

- `QUEUED`
- `CLAIMED`
- `ACCEPTED`
- `ACTIVE`
- `AI_RETURN_PENDING`
- `WRAP_UP`
- `COMPLETED`
- `CANCELLED`

Legal transitions:

| From | To | Authority |
|---|---|---|
| `QUEUED` | `CLAIMED` | PostgreSQL claim transaction. |
| `QUEUED` | `CANCELLED` | Authorized system/supervisor fence. |
| `CLAIMED` | `ACCEPTED` | Current agent token/version. |
| `CLAIMED` | `QUEUED` | Explicit release/requeue with a new generation. |
| `CLAIMED` | `CANCELLED` | Authorized cancellation. |
| `ACCEPTED` | `ACTIVE` | Current owner accepts conversation authority. |
| `ACCEPTED` | `QUEUED` | Explicit transfer/requeue. |
| `ACTIVE` | `WRAP_UP` | Current owner resolves customer-facing work. |
| `ACTIVE` | `AI_RETURN_PENDING` | Human-to-AI intent transaction fences the human and persists the stable P13 identity. |
| `ACTIVE` | `QUEUED` | Explicit transfer/requeue. |
| `ACTIVE` | `COMPLETED` | Resolve without separate wrap-up when policy permits. |
| `AI_RETURN_PENDING` | `COMPLETED` | Exact P13 execution is accepted/bound and AI ownership commits. |
| `AI_RETURN_PENDING` | `QUEUED` | Deterministic known P13 rejection requeues with an explicit failure reason. |
| `WRAP_UP` | `COMPLETED` | Current owner completes after-work activity. |
| Non-terminal | `CANCELLED` | Governed cancellation when no prior action ordering forbids it. |

`COMPLETED` and `CANCELLED` are absorbing. Transfer does not require a separate
`TRANSFER_PENDING` state: the current assignment closes and either a new assignment is
created atomically for a specific agent or the item returns to `QUEUED` for a target queue.

`AI_RETURN_PENDING` does not mean AI ownership. Conversation ownership remains `UNASSIGNED`.
If P13 acceptance is ambiguous, the work item and handoff remain pending for P25 reconciliation.

## Global lock order

The canonical order for all P17 authority mutations is:

1. queue rows, ordered by queue ID;
2. agent-presence rows, ordered by agent user ID;
3. work-item row;
4. conversation-ownership row;
5. assignment rows, ordered by assignment ID;
6. handoff row;
7. action-authorization row.

Transactions skip unused levels but never acquire an earlier level after a later one. A
multi-agent transfer locks both presence rows in ascending agent-user-ID order. A multi-queue
operation locks queues in ascending queue-ID order.

Queue claims take a PostgreSQL `FOR SHARE` lock on the queue row and capture its revision
before locking presence/work authority. Queue disable or routing configuration changes take
`FOR UPDATE`, increment the queue revision and commit atomically. Therefore:

- a claim whose queue lock commits first may complete under the captured revision, and the
  disable/configuration mutation waits;
- a disable/configuration commit that wins first is observed by the later claim, which must
  reject disabled state or re-evaluate the new revision;
- a claim may never validate one revision and commit after a different revision without an
  explicit revision predicate.

This gives queue-disable/configuration-versus-claim one durable first-commit ordering without
serializing independent work-item claims through process-local state.

## Claim, capacity and fencing algorithm

1. Resolve the authenticated Organization and authorized agent.
2. Lock the queue row `FOR SHARE`, capture its revision and verify enabled routing policy.
3. Lock the agent presence/capacity row.
4. Verify explicit claim-eligible presence and remaining capacity.
5. Select one eligible queue item with deterministic ordering and `FOR UPDATE SKIP LOCKED`.
6. Lock conversation ownership and any current assignment in global order.
7. Recheck queue revision, channel/skill policy and conversation ownership generation.
8. Create a unique assignment, new opaque claim token and incremented lease version.
9. Update work item and presence count, append history and insert P04 outbox intent.
10. Commit before returning authority.

Completion and mutation use compare-and-set predicates over Organization, assignment, state,
agent, claim token and lease version. A stale owner changes zero rows and receives a stable
conflict.

P17 does not automatically expire or steal a committed assignment. A future heartbeat may be
diagnostic, but time alone does not transfer authority. Explicit supervisor release is
available; automatic stale-owner recovery belongs to P25.

## AI-to-human handoff

1. P13 requests handoff with tenant, conversation, AI run, bounded reason/context reference
   and correlation ID.
2. One transaction locks the selected queue before conversation ownership in global order,
   verifies `AI` mode and generation, creates or idempotently returns the human work item,
   changes ownership to `UNASSIGNED`, increments generation, appends history and writes P04
   event intent.
3. P13 must verify the ownership generation before any later customer-facing AI output; the
   committed handoff fence blocks a stale AI response.
4. A human claim creates an assignment. Acceptance atomically changes ownership from
   `UNASSIGNED` to `HUMAN`, binds the assignment and increments generation.

Context is referenced, bounded and redacted. P17 never copies credentials, raw authorization,
hidden reasoning or unrestricted provider payloads into the work item.

## Human-to-AI return

1. The current human presents the valid assignment token/version and an authorized return
   reason.
2. Following global lock order, one transaction locks the owning agent-presence row, work item,
   conversation ownership, current assignment and handoff identity, verifies `HUMAN`
   authority, fences/closes the human assignment, decrements active capacity, changes the work
   item to `AI_RETURN_PENDING`, changes ownership to `UNASSIGNED`, increments the pending
   ownership generation and persists one `PENDING_P13_ACCEPTANCE` handoff with a stable P13
   idempotency identity plus event/audit intent.
3. After commit, P17 invokes P13 using that exact tenant, conversation, pending ownership
   generation and stable identity. No human or AI has customer-facing authority while pending.
4. If P13 deterministically rejects before acceptance, P17 locks the work item's target queue
   first and then the remaining authority rows in global order, marks the handoff
   `P13_REJECTED`, keeps ownership `UNASSIGNED`, moves the work item to `QUEUED` with reason
   `AI_RETURN_REJECTED` and requires a fresh human claim/token. This is a closed P17 policy and
   does not require P25.
5. If P13 accepts, P17 binds the exact P13 execution/run identity. In one transaction it
   verifies Organization, conversation, pending generation, stable identity and expected P13
   contract/version; then it marks the handoff `P13_ACCEPTED`, completes the human work item,
   changes ownership to `AI`, increments/binds the resulting AI generation and emits audit/
   outbox intent. Only this commit grants AI authority.
6. If P13 may have accepted but P17 loses or cannot durably persist the result, it marks the
   handoff `P13_ACCEPTANCE_AMBIGUOUS` when safely knowable, otherwise leaves the pending record
   intact. Ownership remains `UNASSIGNED`, the stable identity is reused, human authority is
   not restored automatically and reconciliation belongs to P25.
7. Duplicate return requests with the same semantic identity return the same handoff/binding.
   The same exact P13 run is idempotent; a different run identity is a conflict and cannot
   overwrite the bound execution.

A human send authorization committed before return may complete. If return commits first,
the stale human token cannot authorize another send.

## Human action boundaries

### Messaging

`P17 human action authorization -> P09 MessagingService -> provider adapter`

The final authorization transaction verifies current human ownership, token/version, RBAC,
channel membership and semantic fingerprint while locking work item, ownership, assignment and
action authorization in global order, then commits a stable P09 idempotency key. P17 never
calls WhatsApp, Email or SMS providers directly.

### Voice

P17 stores tenant-aware references to P11 calls and P12 voice sessions and governs who owns
the interaction. P11/P12 retain call state, media bridge, SIP, transport and provider
authority. P17 does not mix media, manage SIP, implement WebRTC or claim that a durable
ownership transition physically reverses already-emitted audio.

### Copilot

Copilot uses P13 with bounded conversation/customer references and an advisory purpose. It
may return a summary, suggested reply, customer context or next-action suggestion. Output is
untrusted advisory data. It cannot directly invoke P09, P08, P07, P11/P12, CRM mutations,
campaigns, transfers or ownership changes.

## Transfer and supervisor semantics

### Agent to queue

The current owner locks the target queue, current-agent presence, work item, ownership and
assignment in global order, validates its token, closes its assignment, updates active
capacity, increments the ownership generation, changes ownership to `UNASSIGNED`, moves the
item to the target queue and returns it to `QUEUED`. The old token is fenced at commit.

### Agent to specific agent

Following global order, the transaction locks any queue first, then both presence rows in
ascending agent-user-ID order, followed by work item, ownership and assignment. It verifies
same tenant, target authorization/presence/capacity, closes the old assignment, creates a new
assignment with a fresh token/version and updates ownership. Token reuse is forbidden.

### Supervisor

`human:supervise` permits inspected, reasoned release, requeue and transfer. The supervisor
does not impersonate the human and cannot reuse the released token. Every mutation captures
the supervisor user, reason and correlation ID in append-only audit history and follows the
same global lock order.

## RBAC proposal

| Permission | Authority |
|---|---|
| `human:read` | Read same-tenant queues, presence, work items, assignments and SLA metadata. |
| `human:work` | Set own operational presence, claim/accept/reply/complete owned work and request transfer/AI return. |
| `human:configure` | Create, update, enable or disable queue policy and bounded capacity configuration. |
| `human:supervise` | Inspect operational state and force release, requeue or transfer with mandatory audit reason. |

Permissions are deny-by-default and Organization-scoped. P09/P13/P11/P12 permissions do not
implicitly grant external callers P17 authority, and P17 permissions do not bypass those
service boundaries.

## API proposal

All request models use `extra="forbid"`, bounded fields and collections, trusted tenant
context, bounded pagination and existing RFC 9457 errors.

- `POST /api/v1/human/queues`
- `GET /api/v1/human/queues`
- `GET /api/v1/human/queues/{queue_id}`
- `PATCH /api/v1/human/queues/{queue_id}`
- `PUT /api/v1/human/presence/me`
- `GET /api/v1/human/presence`
- `GET /api/v1/human/work-items`
- `GET /api/v1/human/work-items/{work_item_id}`
- `POST /api/v1/human/work-items/claim`
- `POST /api/v1/human/work-items/{work_item_id}/accept`
- `POST /api/v1/human/work-items/{work_item_id}/actions`
- `POST /api/v1/human/work-items/{work_item_id}/complete`
- `POST /api/v1/human/work-items/{work_item_id}/transfer`
- `POST /api/v1/human/work-items/{work_item_id}/return-to-ai`
- `POST /api/v1/human/work-items/{work_item_id}/supervisor-release`
- `GET /api/v1/human/work-items/{work_item_id}/transitions`

No body may supply an authoritative `organization_id`, provider credentials, arbitrary URL,
raw SQL, code, shell command or model-provider configuration.

## Deterministic routing and SLA metadata

Queue selection filters enabled queues, closed channel membership, `eligible_at`, bounded
skills/tags and claimable state before applying:

```text
priority DESC, eligible_at ASC, created_at ASC, id ASC
```

Persisted SLA timestamps may include `queued_at`, `first_claim_at`, `accepted_at`,
`first_response_at` and `completed_at`, plus bounded target/status metadata. P17 exposes
operational visibility only; P23 owns billing/metering and P24 owns the full observability and
analytics platform.

## P04 events

Candidate transactional events:

- `human.work.queued`
- `human.work.claimed`
- `human.work.accepted`
- `human.work.transferred`
- `human.work.requeued`
- `human.work.completed`
- `human.work.cancelled`
- `human.handoff.ai_to_human`
- `human.handoff.human_to_ai`
- `human.presence.changed`
- `human.supervisor.released`

Payloads contain versioned envelopes, tenant and opaque entity IDs, bounded state/reason,
correlation and timestamps. They exclude message bodies, customer profiles, credentials, raw
authorization, provider payloads and hidden reasoning.

## Failure and race matrix

| # | Scenario | Commit-order result and durable protection | Deferred boundary |
|---:|---|---|---|
| 1 | Two agents claim one item | Work-item row lock plus unique active assignment lets one commit; loser receives conflict. | None. |
| 2 | One agent concurrently claims at capacity | Presence row serialization and active-count check permit only remaining capacity. | None. |
| 3 | `AWAY`/`OFFLINE` vs claim | Presence mutation and claim serialize on presence row; first commit defines eligibility. | None. |
| 4 | Transfer vs human reply | Reply authorization and transfer lock ownership/assignment; authorization-first may proceed, transfer-first fences old token. | Ambiguous accepted reply reconciliation: P25. |
| 5 | Supervisor release vs reply | Same linearization as transfer; release-first blocks authorization, authorization-first is already logically authorized. | Ambiguous P09 result: P25. |
| 6 | AI handoff vs AI response | Handoff increments ownership generation; P13 response must compare generation before output authorization. | Ambiguous already-emitted output: P25. |
| 7 | Human return-to-AI vs human send | Global lock order serializes both; send authorization-first may proceed, return-intent-first fences the human and leaves ownership `UNASSIGNED` pending P13. | Ambiguous downstream result: P25. |
| 8 | Stale claim token | Compare-and-set over assignment, owner, token and lease version changes zero rows. | None. |
| 9 | Duplicate handoff request | Unique source/generation identity returns the same active work item. | None. |
| 10 | Duplicate accept | Same token/state is idempotent; different token or state conflicts. | None. |
| 11 | Duplicate completion | Same terminal semantic request is idempotent; terminal state remains absorbing. | None. |
| 12 | Browser disconnect | No authority change; assignment remains durable and visible. | Automatic stale-owner handling: P25. |
| 13 | Service crash after claim commit | Claim remains owned with token/version and audit evidence; no silent reassignment. | Reaping/reassignment: P25. |
| 14 | P09 accepts send, local terminal write lost | Replay uses same stable P09 key; no new logical identity. Attempt remains ambiguous. | Automated reconciliation: P25. |
| 15 | P13 accepts AI return, response/linkage lost | Ownership remains `UNASSIGNED`; replay uses the same stable identity, no second logical execution is created and human authority is not restored automatically. | Automated reconciliation: P25. |
| 16 | Queue disabled vs claim | Claim takes queue `FOR SHARE`; disable takes `FOR UPDATE`. First commit determines whether the captured revision may claim or the later claim observes disabled state. | None. |
| 17 | Specific-agent transfer at capacity | Presence rows lock in deterministic order; target capacity must be available at commit. | None. |
| 18 | Cross-tenant queue/agent/conversation reference | RLS and composite foreign keys reject reads and writes even if application validation is bypassed. | None. |
| 19 | Outbox unavailable after state mutation attempt | Business state and outbox intent share one transaction; both commit or neither commits. | Publisher recovery remains P04. |
| 20 | Terminal item receives claim/transfer/reply | State predicate rejects it; terminal states never return to active states. | None. |
| 21 | P13 deterministically rejects AI return | Handoff becomes `P13_REJECTED`; ownership stays `UNASSIGNED`; item returns to `QUEUED` with `AI_RETURN_REJECTED` and requires a fresh human claim. | None. |
| 22 | Duplicate return-to-AI request | Unique semantic return identity returns the same pending, rejected, ambiguous or accepted handoff; it never creates a new P13 key. | None. |
| 23 | Conflicting P13 run returned | Exact binding predicate rejects a run differing from the already-bound run or expected contract/version. | Investigation/reconciliation if prior acceptance is ambiguous: P25. |
| 24 | Queue configuration revision vs claim | Queue `FOR SHARE`/`FOR UPDATE` ordering plus revision predicate makes claim use exactly one committed policy revision. | None. |
| 25 | Duplicate exact P13 binding delivery | Same tenant, conversation, pending generation, stable key and run identity is idempotent. | None. |
| 26 | P13 binding has wrong tenant/conversation/generation | Tenant scope, composite identity and compare-and-set reject it; ownership remains `UNASSIGNED`. | None. |

## Security and abuse controls

- No cross-Organization shared agents or assignments.
- No client-authoritative tenant, owner, claim token generation or lease version.
- No raw SQL/Python/routing expressions, shell, arbitrary URLs or provider configuration.
- No message/provider credentials, unrestricted transcripts or hidden reasoning in queue rows,
  audit or events.
- No copilot auto-send or automatic tool/CRM/call/campaign authority.
- All pagination, queue scans, skill/tag sets, capacity values and context references are
  bounded.
- Claim tokens are opaque, high-entropy and stored/compared using repository security
  conventions; they are never logged or emitted in P04 events.
- Human actions require both RBAC and current durable ownership; authentication alone is not
  work authority.

## P25 recovery boundary

P17 persists assignment identity, owner, token/version, acquisition time, downstream stable
idempotency keys, external references and ambiguous status. P17 supports explicit authorized
release, requeue and transfer. It does **not** automatically:

- expire and steal assignments;
- reap disconnected agents;
- reassign orphaned work;
- reconcile P09 accepted-but-unrecorded sends;
- reconcile P13 accepted-but-unrecorded AI returns;
- perform distributed failover.

Deterministic P13 rejection before acceptance is not a P25 case: P17 records rejection and
requeues for a new human claim. Only possible P13 acceptance whose exact result cannot be
determined or persisted crosses the P25 boundary. Those ambiguous recovery mechanisms require
the separately governed NXS-P25 resilience contract.

## Explicit exclusions

NXS-P17 does not implement predictive dialing, workforce forecasting, payroll, shift
scheduling, QA scoring, speech analytics, screen or call recording, live media mixing,
WebRTC, SIP routing, telephony/media transport, messaging/provider SDKs, LLM providers,
generic workflow execution, campaign orchestration, recurring campaigns, copilot auto-action,
cross-Organization shared agents, external ticket synchronization, infrastructure/cell
autoscaling, billing, global observability, disaster recovery, frontend applications or
production deployment.

## Implementation certification prerequisites

Future P17 implementation must start through the canonical lifecycle only after this
governance change merges to exact-green `main` and explicit human authorization is granted.
Certification will require migrations, forced-RLS/schema proof, real PostgreSQL concurrency
tests, tenant adversarial tests, boundary integration tests, failure/race coverage, full
quality/security gates, clean-room reproduction and exact-head GitHub CI/Security. Governance
alignment alone leaves `NXS-HUMAN-001` PLANNED and NXS-P17 PLANNED/PENDING.
