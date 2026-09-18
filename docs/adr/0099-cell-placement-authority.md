# ADR-0099: Cell placement authority without tenant or execution-identity replacement

Status: approved and implemented P18 contract. The feature branch was certified READY/GO
at `c2de25360aff1644528f2ce314e98403cb3fdbf4`; its documentation-only corrective
subsequently passed external audit and authorized canonical reclosure. Current branch
state is READY/GO, bound to `cfa98bd76c0b0e00653356651d672b4e213ef630`, pending final
external post-reclosure audit and merge.
Runtime and architectural decisions are unchanged. Canonical main remains through P17.

## Context

ADR-0009 and ADR-0017 establish horizontal Cells and stateless compute as the target.
P13 and P17 now depend on precise PostgreSQL execution ownership, permits and
generations. A routing lookup or cache cannot replace those authorities, and an
assignment generation alone cannot safely relocate active external work.

## Decision

Retain Organization/organization_id as the tenant. A durable platform Cell catalog
and one tenant-owned current placement per Organization live in the existing
PostgreSQL authority domain. Forced RLS and tenant composite FKs remain mandatory.
Initial placement and same-Cell suspension/reactivation are P18 scope. Different-
Cell reassignment, database sharding and live media/data migration are rejected.

Use monotonic assignment_generation for placement fencing, separate from domain
claim tokens and conversation ownership generations. One platform resolver returns
a snapshot; one reusable transactional admission guard validates the tuple under
shared placement locks before domain claims/permits. Exclusive placement mutation
serializes with admission. Existing domain lock orders remain nested underneath
placement locks. No transaction spans provider I/O.

Tenant mutation receipts, history and P04 outbox intent commit atomically. Neither
cache nor events grant authority. Logical execution keys never change because of
routing. Authorized-before-suspension effects may finish; suspension-first blocks
new authorization. Ambiguity does not create another owner or identity.

## Consequences and current versus target

This ADR originated as the pre-implementation authority contract, detailed in
`../engineering/nxs-p18-cell-scaling-design.md`. That contract is implemented by
`b655e615b18aafec4f7a1cc57e25bd97cf6b0a79` and its original certification evidence is
preserved under `.nxs/evidence/NXS-P18/`; see `../engineering/nxs-p18-runtime.md` for
the implemented boundaries. It is not a capacity or availability certification.
Registration expands placement choices for new Organizations;
existing assignments cannot be live-moved. P25 recovery and P28 capacity remain
separate. No existing ADR or certified execution boundary is superseded.
