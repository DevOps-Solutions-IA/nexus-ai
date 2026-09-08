# ADR-0064: Tool invocation idempotency

Status: Accepted. Context: NXS-TOOL-001 requires idempotency controls for side-effecting tools. The directive is explicit: do not claim exactly-once execution; use durable request fingerprinting; a repeated key with the same fingerprint may replay the stored result; a repeated key with a different fingerprint must fail deterministically.

Decision:

**Three idempotency policies.** A tool declares `NONE`, `OPTIONAL` or `REQUIRED`. A tool whose `side_effect_class` is `NON_IDEMPOTENT_WRITE` or `EXTERNAL_EFFECT` may not declare `NONE` — the registry rejects that combination at register and update time. When policy is `REQUIRED` and the side effect is one of those two classes, an invocation without an `idempotency_key` is rejected with `NXS_TOOL_IDEMPOTENCY_REQUIRED` before any external call.

**Durable claim-based store.** `tool_idempotency_records` is a `TenantOwnedMixin` table with forced RLS, `UNIQUE(organization_id, tool_id, idempotency_key)` and a composite tenant-aware FK to `tool_definitions` with `ON DELETE CASCADE`. `ToolIdempotencyRepository.claim` uses the P06 race-recovery pattern: a lost unique-constraint race propagates the wrapped conflict out of the transaction (full rollback, no orphan), then a fresh transaction re-reads the committed winner.

**Fingerprint.** `request_fingerprint(tool_key, tool_version, merged_arguments)` is a SHA-256 over canonical sorted-key JSON of the arguments *after* static-argument merge. Same key + same fingerprint + a `COMPLETED` record → the stored `ToolResult` is replayed with `replayed=True` and no upstream call. Same key + a `PENDING` record → `NXS_TOOL_EXECUTION_IN_PROGRESS` (retryable). Same key + a *different* fingerprint → `NXS_TOOL_IDEMPOTENCY_CONFLICT` (never a silent second execution).

**Integration Hub key derivation.** The engine derives its own P07 idempotency key, `t-<sha256(tool_id:caller_key)[:40]>`, so the Hub's own idempotency layer (ADR-0060) also collapses duplicate downstream calls, and a caller key is never reused verbatim across tools.

**Finalisation.** The record is finalised on both outcomes: `COMPLETED` with the result JSON on success, `FAILED` with the error code on a raised `NxsError`. A `FAILED` record does not replay — a later same-key call re-executes.

Consequences: concurrent identical calls converge on one upstream execution and one execution record; a client that retries after a partial failure gets a deterministic answer, not a duplicate side effect; the guarantee offered is "durable de-duplication", never "exactly once".
