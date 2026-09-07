# Engineering Standards

## Python and boundaries

Python 3.14 is the single deliberate production baseline (`requires-python`, Ruff `target-version`, mypy `python_version`, the CI matrix and the container image all agree) and is locked with `uv`; production and development dependencies remain separated. Ruff formatting/linting, strict mypy, warnings-as-errors pytest, and branch coverage are mandatory. Public functions and boundaries are typed. Async is reserved for real concurrent I/O; blocking work must not run on the event loop. Domain packages own their invariants and communicate through versioned contracts rather than database-table coupling.

APIs use explicit schemas, stable error codes, correlation IDs, bounded pagination, idempotency keys for retried mutations, and backward-compatible additive evolution. Errors preserve safe diagnostic context but never secrets. Configuration is validated at startup and supplied through environment or a future secret broker; no hidden defaults for security-sensitive settings.

## Data and distributed systems

Every migration is forward-only, transaction-aware, backward-compatible with the prior deployed version, tested on realistic data, and paired with rollback/roll-forward instructions. External calls use bounded deadlines, exponential backoff with jitter only for retry-safe failures, circuit breaking where justified, and idempotency. Ownership, ordering, duplicate delivery, partial failure and consistency expectations must be documented. Stateless compute is preferred; durable state belongs in declared stores.

## Security and observability

Least privilege, tenant isolation, input validation, output encoding, dependency review and credential indirection are required. Logs are structured, redacted, bounded and correlated; metrics avoid high-cardinality tenant/customer labels. New critical paths define SLIs, failure signals, dashboards and alerts. Feature flags need owners, expiry and safe defaults.

## Tests, compatibility and release

Changes test success, failure, authorization, concurrency and recovery paths proportional to risk. Contract and migration changes test old/new interoperability. Never blanket-ignore lint, types or warnings. Deprecations publish replacement and removal windows. Each PR documents rollback, operational effects and evidence; commits are atomic and branches are phase-scoped. Releases originate from GitHub artifacts—never manual production VPS edits.
