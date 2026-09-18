# P18 implementation — prior certification, documentation corrective pending

This feature-branch implementation follows ADR-0099 and the approved P18 design.
Canonical main remains certified through P17. P18 runtime was implemented at
`b655e615b18aafec4f7a1cc57e25bd97cf6b0a79` and certified READY/GO at
`c2de25360aff1644528f2ce314e98403cb3fdbf4`, both preserved on the unmerged branch.
The current phase is VALIDATING/PENDING for a documentation-only corrective; runtime
is unchanged, and external corrective audit plus separate reclosure authorization
are required. Prior certification covers placement correctness, not deployment,
capacity, failover or physical exactly-once. Historical evidence is not a new GO.

## Durable structures and control boundary

Migration `c18a0b1c2d3e`, following P17 `f17a0b1c2d3e`, adds:

- `cells`: global immutable UUID/key and registration receipt, REGISTERED/RETIRED.
- `cell_control_history`: append-only global registration/retirement receipts,
  operation/key uniqueness and semantic fingerprints; platform-control read/write.
- `organization_placements`: forced-RLS current Organization placement, immutable
  Cell, ACTIVE/SUSPENDED and monotonically increasing `assignment_generation`.
- `placement_mutations`: forced-RLS combined history/receipt, composite
  `(organization_id, placement_id)` reference, operation/key uniqueness and fingerprint.

The two placement unique constraints are deliberately different: Organization
uniqueness prevents two current placements; `(organization_id, id)` uniqueness
provides the composite foreign-key target. Catalog inspection and a PostgreSQL
cross-tenant negative insertion test verify the actual columns, not their names.

The insert trigger sets the catalog's irreversible `has_placements` latch. Since
runtime placement deletion and relocation are prohibited, this prevents retirement
of even suspended bound Organizations without a cross-tenant RLS bypass. Retirement
and initial assignment serialize on the catalog row. Retirement is absorbing and
same-key replay returns the same result without another history row. A changed
fingerprint conflicts. There is no automatic reassignment or health-based decision.

Only an active user with explicit platform `cell:control` can administer Cells or
placements. No Organization role implies that grant. Live authentication is required
by the HTTP boundary; tenant scope for placement comes from trusted authentication,
not body/header Organization identifiers. Global catalog rows contain only safe IDs,
keys and administrative state, not credentials, destinations or tenant contents.

The bounded control surface is:

| Method | Path | Meaning |
| --- | --- | --- |
| POST | `/api/v1/cells` | Idempotent registration; server-generated identity |
| GET | `/api/v1/cells` | Keyset page: UUID `after_id`, limit 1–100 |
| GET | `/api/v1/cells/{cell_id}` | Platform-authorized inspection |
| POST | `/api/v1/cells/retire` | Terminal, idempotent unbound retirement |
| GET | `/api/v1/cell-placement/current` | Inspect trusted current Organization |
| POST | `/api/v1/cell-placement/current` | ASSIGN/SUSPEND/RESUME with generation/key/reason |

There is no fleet-wide tenant listing, relocation endpoint, client-selected execution
Cell, arbitrary metadata map, provider configuration or network destination surface.

## Admission and rollout

`NXS_CELLS__WORKER_CELL_ID` is frozen server configuration. No HTTP header sets it.
`PlacementResolver` returns a snapshot only. Internal delivery can bind that snapshot
with `placement_route`; it still cannot grant authority without the configured worker
Cell and a current PostgreSQL match. Forged Organization/Cell/generation tuples fail.

`Database.execution_transaction` obtains placement admission before domain locks:
catalog FOR SHARE, placement FOR SHARE, then the original subsystem lock order.
The exact snapshot, ACTIVE state and worker Cell must match through commit. Mutation
takes exclusive catalog/Organization/placement locks. No transaction spans provider I/O.
No cache or event infrastructure is consulted to authorize execution.

The explicit pre-placement rollout seam preserves bootstrap/authentication and
existing unassigned Organizations without inventing a default Cell. An unconfigured
worker can serve only an Organization that has no placement, under an Organization
shared lock that serializes with initial assignment. As soon as assignment commits,
that worker is fenced: it cannot silently bypass the placement. A configured Cell
worker rejects an unassigned Organization. Operators must register the intended Cell,
configure its workers, and perform bounded, audited initial assignments. Assignment
does not migrate data, reap old claims or revoke already-authorized external work.

## Protected runtime boundaries

| Subsystem | Transactions acquiring placement admission |
| --- | --- |
| P13 | New session, turn owner, model permit and tool permit |
| P17 | Handoff, claim, accept, human/queue transfer, supervisor transfer, AI return request and exact binding, human send authorization, AI output check, advisory copilot entry |
| P14 | Run creation, step claim, result/retry transactions that can unlock successor steps |
| P15 | Due materialization and occurrence claim; P14 independently admits downstream start |
| P16 | Recipient claim/workflow start, release activation/materialization, recipient workflow confirmation and final send permit |
| P09 | Outbound idempotency claim, or queued-message authorization for sends without an idempotency key |

Configuration, authentication, reads and safety-revocation operations are not replaced
with Cell authorization. Domain permissions, composite references, cancellation and
original semantic identities remain mandatory. `cell_id != authorization`.

P09 result/finalization and P17 consumed-result/SLA writes can record an already
authorized send after suspension; they do not grant new work. First-response remains
the original once-only PostgreSQL timestamp on consumed P09 success. If a workflow
result would unlock successor work after suspension, it is fenced and the original
RUNNING claim remains durable rather than being falsely failed or redispatched.
Reconciliation of such ambiguity remains P25.

Every downstream boundary that grants a *new* authority independently admits it.
A P16/P17 logical permit is not a bypass of a later P09 new-send authorization.
An already-committed P09 authorization may finish; a suspension that wins before
that authorization blocks it. No physical delivery cancellation is claimed.
For keyed P09 sends, the successful durable idempotency claim is that authorization;
the original owning call can persist its queued message afterward without a second
placement grant. A replay cannot impersonate that owning call: P09 returns its stored
result or in-progress/failure outcome rather than entering the provider again.

P17 AI return remains UNASSIGNED/pending if placement is suspended before exact
P13 binding. A later valid same-key retry binds the same accepted session, never a
fresh execution identity. Known rejection and original P25 ambiguity policies remain
domain-owned. Placement generation never resets ownership/lease/claim identities.

## Events, failures and evidence

Version-one `cell.assignment.created`, `.suspended` and `.resumed` events use the
existing P04 envelope, registry and tenant outbox. Placement, receipt/history and
event intent commit together. Global inventory uses control history, not a fabricated
global tenant-outbox contract. No P18 projection consumer is required: event replay,
unsupported versions, stale routing hints and cache/bus outage cannot grant authority.

Tests use PostgreSQL runtime sessions, explicit barriers/lock-conflict assertions and
instrumented P09/P13 boundaries. They cover committed-response loss, durable queued
work after worker disappearance, no orphan takeover, and exact P14/P15/P16 identities.
`python -m scripts.nxs_p18_evidence` executes the criteria suite using the existing
phase-evidence command runner, records pytest call/setup/teardown outcomes and fails
on absent, skipped or failed mapped tests. Source hashes bind the uncommitted candidate
without inventing a self-referential implementation SHA. `make nxs-gate PHASE=NXS-P18`
records canonical quality results; neither command closes the phase.

The migration adds no placement data. Clean, canonical-schema upgrade and isolated
downgrade/re-upgrade are executable tests. Downgrade destroys P18 tables and grants;
it is for disposable validation only, not a production rollback plan. Deployed changes
would require reviewed roll-forward/data-preservation procedures.

Cross-Cell relocation, recovery/failover, SIP/Kamailio, capacity certification,
frontend, infrastructure provisioning, deployment and later-phase activation remain
excluded. Cell unavailability can make assigned work unavailable; that is a deliberate
safety tradeoff, not an availability certification.
