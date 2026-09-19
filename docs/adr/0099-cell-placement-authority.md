# ADR-0099: Cell placement authority without tenant or execution-identity replacement

Current status: P18 READY/GO on canonical main after merge
`e2114cfe8f150e85b9ae432a9af557ceb52cf836`. Original runtime
`b655e615b18aafec4f7a1cc57e25bd97cf6b0a79`, corrective candidate
`cfa98bd76c0b0e00653356651d672b4e213ef630` and reclosure
`9c6949aca7b53c1d4c4c8839933ad8b2a7a66c71` remain preserved.
P19 is PLANNED/PENDING in governance preparation; no production deployment occurred.

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
