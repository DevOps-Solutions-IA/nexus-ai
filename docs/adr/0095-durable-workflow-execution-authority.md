# ADR-0095: Durable Workflow Execution Authority

Status: Accepted for NXS-P14

## Context

Nexus AI workflows coordinate tenant-owned tool and agent operations across independent backend replicas. Process-local queues and locks cannot prove single execution authority, survive restart, order cancellation, or prevent a stale worker from overwriting a newer outcome. External effects also cannot honestly be described as physically exactly-once.

## Decision

PostgreSQL is authoritative for workflow and step state. Every workflow table carries `organization_id`, forced RLS, and tenant-aware composite foreign keys. A run binds to an immutable published version. The engine locks the run row before claim/control transitions and locks a ready step with `FOR UPDATE SKIP LOCKED`. A successful claim persists an owner UUID, opaque token, attempt number, and timestamp before external execution begins.

Completion and failure require the expected active state, owner UUID, and claim token. Cancellation uses the same run lock, becomes absorbing, and prevents every new claim. Pause blocks new claims; an already-authorized attempt may record an outcome, but cannot unlock later work until resume. Retry ceilings and retryable error codes are persisted.

`TOOL` steps invoke NXS-P08 only, with a stable semantic idempotency identity. `AGENT` steps invoke NXS-P13 only. Conditions use a constrained data-path/operator grammar and never `eval`. No workflow configuration may select an arbitrary URL, SQL, shell, provider, credential, scheduler, campaign, or human queue.

P14 records ambiguous active execution after a crash. It does not automatically reap, reassign, redispatch, or reconcile that execution. Those policies belong to NXS-P25.

## Consequences

- Multiple workers may contend safely without sharing memory.
- Commit order gives claim, pause, resume, and cancellation a provable ordering.
- A stale worker cannot commit after cancellation or with an obsolete token.
- External exactly-once behavior is not overclaimed; stable downstream identities reduce duplication while ambiguity remains visible.
- Operators may need P25 reconciliation for a step left active by a dead worker.
- Published versions and transition history are retained and protected from destructive runtime operations.

## Current Versus Target

NXS-P14 implements the durable schema, claim/finish fencing, bounded retries, lifecycle controls, P08/P13 delegation, API, RBAC, P04 events, and adversarial tests. NXS-P15 will own time-based scheduling. NXS-P24 will expand observability. NXS-P25 will own automatic crash recovery and ambiguous-effect reconciliation. NXS-P32 will own deployment.
