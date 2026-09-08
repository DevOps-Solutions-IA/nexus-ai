# ADR-0059: Untrusted response validation and the Integration error taxonomy

Status: Accepted. Context: NXS-INT-001 receives responses from systems Nexus does not control. Those responses are untrusted input, and the failure modes must map to a small, stable set so callers (and the future Tool Engine) can branch deterministically. Decision:

**Response validation pipeline** (`nexus_ai.integrations.result`). Before any value reaches a caller a raw response passes, in order:

1. HTTP status classification — `2xx` in the operation's `success_status` continues; `429` → `NXS_INT_UPSTREAM_RATE_LIMITED` (with `Retry-After` parsed when present); `5xx` → `NXS_INT_UPSTREAM_SERVER_ERROR` (retryable); `4xx` → `NXS_INT_UPSTREAM_CLIENT_ERROR` (terminal); any other unexpected status → `NXS_INT_RESPONSE_INVALID`;
2. content-type check against the operation's declared type;
3. body-size check (already bounded by the executor's streamed limit);
4. strict JSON parse (UTF-8);
5. JSON Schema check against the operation's optional response schema, with required-field and type enforcement.

`204` / empty bodies decode to `null`. Anything that fails becomes a stable error — nothing is half-parsed and handed on.

**Normalized `IntegrationResult`.** Every execution returns (or, for a handled failure the Hub chooses to surface, returns rather than raises) a single shape: `result_class` (`SUCCESS`, `UPSTREAM_CLIENT_ERROR`, `UPSTREAM_SERVER_ERROR`, `UPSTREAM_RATE_LIMITED`, `TRANSPORT_ERROR`, `RESPONSE_INVALID`, `POLICY_BLOCKED`, `CIRCUIT_OPEN`, `RATE_LIMITED`, `IDEMPOTENCY_CONFLICT`, `CONFIG_ERROR`, `EXECUTION_FAILED`), `ok`, a canonical `status_code`, validated `output`, `upstream_status`, `error_code`, `retry_count`, `duration_ms`, `config_revision`, `correlation_id`, `idempotency_key`, `replayed`. It round-trips through `model_dump` / `model_validate` (the durable idempotency replay depends on it).

**Error taxonomy** (`nexus_ai.integrations.errors`, ~28 categories). Every failure maps to exactly one stable `NXS_INT_*` code with an HTTP status, a safe title and a deliberate `retryable` flag; all render to RFC 9457 Problem Details through `nexus_ai.core.problem_details` like every other `NxsError`. The taxonomy is coarse and closed: an untrusted upstream response, a policy rejection, a transport fault and a configuration mistake are each ONE category. Provider-specific detail lives only in bounded `extensions` (e.g. `upstream_status`, `retry_after_seconds`, `reason`) — never a new error class per provider.

**Structured logging.** Every execution logs `integration_id`, `operation_key`, `organization_id`, `config_revision`, `duration_ms`, `result_class`, `retry_count`, `upstream_status`, `correlation_id`. It NEVER logs `Authorization`, an API key, a password, a token, a webhook secret, a sensitive response body or a sensitive query string; configured sensitive header names are redacted.

Consequences: a caller sees a bounded, schema-checked object on success and one of a fixed set of codes on failure; an integration cannot leak an unexpected shape, an oversized body or a provider's raw error into Nexus.
