# ADR 0097: Durable campaign send authority

- Status: Accepted for NXS-P16
- Date: 2026-09-14
- Requirement: `NXS-CAMP-001`

## Context

Campaign delivery combines bulk audience processing with mutable consent, suppression,
pause, cancellation, throttling, workflow completion, and an external messaging effect.
Holding a database transaction across provider I/O is unsafe, but checking policy and
then calling P09 without a durable authorization boundary creates a time-of-check/time-of-
use race. Multiple backend replicas must also claim recipients without duplicate logical
workflow or message execution.

## Decision

PostgreSQL is authoritative for campaign revisions, sealed audience membership,
recipient attempts, policy epochs, throttle reservations, and send permits. Workers use
row locks and opaque owner/claim tokens. Every P14 and P09 call receives a separate stable
idempotency key derived from one tenant/campaign/revision/run/recipient/channel identity.

The final authorization transaction locks the run, campaign, attempt, recipient and
authoritative policy rows; revalidates consent, suppression, destination, quiet hours and
throttle capacity; and creates one tenant/attempt-bound permit. The commit that records
the permit as `AUTHORIZED` is the logical-send linearization point. Only then may the
worker invoke `MessagingService.send`. P09's existing durable idempotency handles replay
after an accepted response whose P16 terminal write was lost.

Consent or suppression committed before authorization blocks the permit. A mutation
committed after authorization governs future sends but does not retroactively revoke the
already authorized logical send. Campaign pause/cancel uses the campaign row as the
shared serialization boundary. No transaction remains open during provider I/O.

P14 workflow completion is a prerequisite, never a send permit. P16 cannot call provider
adapters, P08 tools, arbitrary URLs, shell, SQL, or model providers. P09 remains the only
outbound messaging boundary.

## Consequences

- Physical exactly-once delivery is not claimed; one stable logical identity is.
- An authorized-but-unconsumed permit is durable ambiguity, not silently reassigned.
- P25 owns automated stale claim recovery and ambiguous-effect reconciliation.
- Sealed audience facts and revision specifications are database-guarded against mutation.
- Bounded materialization and claims avoid all-audience transactions and process-memory
  authority.
