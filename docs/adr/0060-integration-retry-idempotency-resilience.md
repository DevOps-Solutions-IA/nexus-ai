# ADR-0060: Retry classification, execution idempotency, circuit breaker and outbound rate limiting

Status: Accepted. Context: NXS-INT-001 calls external systems that fail transiently, rate-limit, and occasionally process a request twice if retried carelessly. Nexus must retry safely, deduplicate durably, protect a failing provider and cap its own outbound volume — without claiming exactly-once. Decision:

**Retry classification.** An operation declares a `RetryClass`:

- `SAFE` — no side effects (a GET): retry on any transient failure, including a connection error that may have partially sent;
- `IDEMPOTENT` — a repeat is harmless (PUT / DELETE, or POST with a provider idempotency key): retry on in-flight failures, `5xx` and `429`;
- `NON_IDEMPOTENT` — a repeat could double an effect (a plain POST): retry ONLY when the request provably never reached the server (`PRE_SEND` — DNS / connection-refused / TLS), never after a timeout or a `5xx`.

Backoff is exponential with full jitter (`secrets`-based, no insecure RNG), capped by `retry_max_delay_seconds` and by a total `retry_max_elapsed_seconds` budget and `retry_max_attempts`. An upstream `Retry-After` always wins, clamped to the max delay.

**Durable execution idempotency.** Scoped by `(organization_id, integration_id, operation_key, idempotency_key)`. A request fingerprint (SHA-256 over the canonical operation key + input) is stored with a `PENDING` claim (`integration_idempotency_records`, `UNIQUE(...)`). Same key + same request → the stored `COMPLETED` result is replayed (`replayed = true`); same key + different request → deterministic `NXS_INT_IDEMPOTENCY_CONFLICT`; a claim still `PENDING` (in flight elsewhere, or a crash) → retryable `NXS_INT_EXECUTION_IN_PROGRESS` until finalised or the retention window lapses; a terminal `FAILED` claim → `NXS_INT_EXECUTION_FAILED` (use a new key). This is NOT exactly-once — a crash between "send" and "record COMPLETED" can still re-send a `NON_IDEMPOTENT` operation once; the retry policy already forbids retrying such an operation after it may have reached the server.

**Circuit breaker.** Per `(organization_id, integration_id, operation_key)`, tenant-scoped by construction. Standard `CLOSED → OPEN → HALF_OPEN`: `circuit_failure_threshold` consecutive failures open it; while `OPEN` calls are refused fast with `NXS_INT_CIRCUIT_OPEN` (no upstream hit) until `circuit_reset_seconds` elapses; `HALF_OPEN` admits a bounded number of trials — one success closes, one failure re-opens. Scope is bounded to this process (cross-node breaker coordination is NXS-P25); state is guarded by a per-key `asyncio.Lock` and the key table is capped.

**Outbound rate limiting.** Per organization (optionally per integration), a fixed-window counter that reuses the P03 cache pattern — a shared Valkey counter when connected (cross-process), degrading to a bounded in-process counter otherwise. Exceeding it is `NXS_INT_OUTBOUND_RATE_LIMITED` (retryable, `retry_after_seconds`). This is a protective ceiling on Nexus-initiated volume — NOT cost metering or billing (NXS-P23), which owns its own accounting. An upstream `429` is normalised separately by the response validator.

Consequences: a transient outage costs a bounded number of retries within a bounded time; a duplicated caller request returns the same result; a persistently failing provider is isolated within seconds without hammering it; a runaway caller cannot flood a provider from Nexus.
