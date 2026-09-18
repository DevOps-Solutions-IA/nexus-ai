# ADR-0099: Cell placement authority without tenant or execution-identity replacement

Status: Proposed for NXS-P18 governance review; not implemented.

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

This is a future implementation contract, detailed in
`../engineering/nxs-p18-cell-scaling-design.md`, not a capacity or availability
certification. Registration expands placement choices for new Organizations;
existing assignments cannot be live-moved. P25 recovery and P28 capacity remain
separate. No existing ADR or certified execution boundary is superseded.
