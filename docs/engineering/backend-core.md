# Backend Core

The production backend is built by `create_app(settings: Settings | None = None)` in
`nexus_ai.application`; `nexus_ai.main:app` is the ASGI entrypoint and opens no
connections at import. Layout: `api/` (router, dependencies, middleware, error rendering,
system endpoints, contract primitives), `core/` (config, context, errors,
problem_details, health, lifecycle, logging, metadata, telemetry, identifiers) and
`infrastructure/` (database, cache, messaging, orm).

## Configuration

All settings come from the environment with the `NXS_` prefix and `__` nesting
(`NXS_DATABASE__POOL_SIZE=10`). Models are frozen and validated: bounded pools and
timeouts, validated DSN/URL schemes, `SecretStr` secrets with redacted `safe_dsn`/
`safe_url`. Environments: `local`, `test`, `staging`, `production`. Production refuses to
start with wildcard allowed-hosts, wildcard CORS, enabled docs, `telemetry.mode=disabled`
or a missing required `NXS_DATABASE__DSN` / `NXS_CACHE__URL` / `NXS_MESSAGING__URL`.
`get_settings()` is a cached singleton; tests call `get_settings.cache_clear()`.

## Startup and shutdown

`ApplicationLifespan.startup()` loads settings, configures logging and telemetry, builds
the `Database`, `Cache` and `Messaging` adapters and opens connections. A missing
required URL/DSN fails startup; a reachable-but-down dependency does not — the process
stays live and `/health/ready` returns 503 until it recovers. `shutdown()` drains NATS,
then Valkey, then PostgreSQL, flushes telemetry, and is safe to call more than once.
SIGTERM is handled by uvicorn and drains within the configured timeout.

## Health

`GET /health/live` — process liveness, never touches a dependency, always 200.
`GET /health/ready` — `200 READY` / `503 NOT_READY` with a typed per-dependency list
(`name`, `status` ∈ UP/DOWN/DEGRADED/UNKNOWN, `required`, `latency_ms`,
`last_error_code`); results are TTL-cached (`NXS_HEALTH__CACHE_TTL_SECONDS`). Legacy
`GET /health` (`{"status":"ok"}`) and `GET /version` are preserved from P00.

## API and errors

Versioned surface under `/api/v1`; `GET /api/v1/system/version` returns safe service
metadata. OpenAPI is exposed only when `NXS_HTTP__DOCS_ENABLED` is true. Every error is
`application/problem+json` (RFC 9457) with a stable `NXS_*` `code` and the `request_id`;
no stack traces. Validation errors are normalised to bounded field entries. Pagination
(`PageParams`, `Page[T]`) and the `Idempotency-Key` contract live in
`nexus_ai.api.contracts`.

## Request context and logging

`RequestContextMiddleware` assigns a request id and a separate correlation id (accepted
from `X-Request-ID` / `X-Correlation-ID` when they match the allow-pattern, else
generated), echoes them, binds them into `contextvars` and the log context, rejects
oversized headers (431) and emits one structured access line per request.
`SecurityHeadersMiddleware` sets `nosniff`, `DENY`, `no-referrer`, `no-store` and (in
production) HSTS, and minimises the server banner. Logging is `structlog` JSON with
`timestamp`/`severity`/`service`/`environment`/`event` plus correlation; sensitive keys
and URL credentials are redacted and field values are length-bounded. stdlib loggers
(uvicorn, SQLAlchemy) render through the same pipeline.

## Dependencies (PostgreSQL, Valkey, NATS)

`Database` exposes `session()` (rollback on error, always closes) and `transaction()`
(commit/rollback unit of work, no hidden autocommit); inject `DbSessionDep`. `Cache`
wraps the async Redis-protocol client; `Messaging` wraps NATS with reconnect callbacks
and JetStream detection. All three probe with a hard timeout and expose safe health.
Alembic is wired to the validated DSN — `make migrate` / `make migrate-check`.

## Telemetry

`NXS_TELEMETRY__MODE`: `disabled` (no-op, default), `local` (console spans), `export`
(OTLP/HTTP, requires `NXS_TELEMETRY__OTLP_ENDPOINT`). Never use high-cardinality labels
(customer id, phone, email, message/conversation id) as metric labels — a permanent NXS
rule. Full observability is P24.
