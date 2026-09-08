# ADR-0062: Tool registry and versioning model

Status: Accepted. Context: NXS-TOOL-001 requires tool definitions to be deterministic and versioned, tenant-owned, and to declare a fixed set of attributes before they can be invoked.

Decision:

**A tool is a tenant-owned facade over exactly one Integration Hub operation.** `tool_definitions` is a `TenantOwnedMixin` table with forced RLS (`ENABLE` + `FORCE`, `USING` + `WITH CHECK`). Every row declares: stable `id` (UUIDv7), `organization_id` (canonical scope), `tool_key` (stable, `^[a-z][a-z0-9_.-]{1,62}$`, `UNIQUE(organization_id, tool_key)`), `version` (int, `>= 1`), `name`, `description`, `status`, `risk_class`, `side_effect_class`, `idempotency_policy`, `timeout_seconds`, `input_schema` (closed JSON Schema object), `output_schema` (open, nullable), `required_permissions`, `binding_type` (`INTEGRATION` only in P08), `integration_id`, `operation_key`, and `static_arguments`. A CHECK constraint enforces that an `INTEGRATION` binding has both `integration_id` and `operation_key`.

**The binding is a database boundary.** `(organization_id, integration_id) → integrations(organization_id, id)` is a composite tenant-aware FK with `ON DELETE RESTRICT` (ADR-0052 pattern), so the database itself refuses a cross-tenant binding and refuses to drop an integration a tool still uses. Registration additionally verifies the integration exists and declares the named `operation_key`, wrapping a miss as `NXS_TOOL_BINDING_INVALID`.

**Lifecycle.** `DRAFT → ACTIVE → DISABLED` (plus `ERROR` for a future health signal). A tool is created `DRAFT`; activation re-checks the risk ceiling and the binding. Only `ACTIVE` tools invoke. `set_status` accepts `ACTIVE` or `DISABLED` only — never a jump back to `DRAFT`.

**Deterministic version bumps.** `version` increments when and only when a semantically significant field changes: `input_schema`, `output_schema`, `binding`, `side_effect_class`, `risk_class` or `required_permissions`. Cosmetic edits (`name`, `description`, `timeout_seconds`) do not bump. A no-op update returns the current definition unchanged. Concurrent updates are serialised by the row and remain monotonic — no lost update.

**Events.** `tools.registered / updated / disabled` — registered, versioned, strict, ID-only P04 outbox payloads enqueued in the same transaction as the mutation. No schema body or binding detail reaches the bus.

Consequences: a caller that resolved `tool_key` at version N gets a coherent definition; an operator can evolve a tool without breaking audit attribution, because every execution record stores the `tool_version` it ran against.
