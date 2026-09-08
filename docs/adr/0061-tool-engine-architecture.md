# ADR-0061: Tool Engine architecture

Status: Accepted. Context: NXS-TOOL-001 requires that AI-requested tools execute through policy, scoped credentials, audit and idempotency controls. The future Agent Runtime (NXS-P13) and Workflow Engine (NXS-P14) both need a single governed way to turn a model's "call this tool with these arguments" into a verified outcome, without either coupling to an LLM vendor or ever exposing a raw API, URL, header set or secret to the model.

Decision:

**One governed pipeline.** Every invocation follows exactly one route: `caller → Tool Registry (resolve by tool_key) → enabled/version check → caller principal → RBAC / required permissions → organization + risk policy → argument schema validation → side-effect / idempotency rules → Integration Hub (NXS-P07) execution → output schema validation → execution receipt + event`. `ToolEngine.invoke(principal, ToolInvocation)` is the only entry point. `ToolEngine` is implemented in `src/nexus_ai/tools/service.py` as a fixed 14-step deterministic order; the steps never reorder and never short-circuit past authorization.

**The invocation contract is minimal by construction.** `ToolInvocation` carries `tool_key`, `arguments`, an optional `idempotency_key` and an optional `correlation_id` — nothing else. It is `extra="forbid"`. There is no `integration_id`, `url`, `method`, `headers`, `query`, `operation_key`, `graphql` or `organization_id` field, so a model cannot smuggle an execution primitive or a forged tenant through it. The trusted Organization is always `principal.organization_id`.

**No arbitrary-execution endpoint.** The API exposes governed registry CRUD plus `POST /api/v1/tools/invoke`. There is deliberately no `run-anything`, `/http/request`, raw shell, arbitrary Python, arbitrary SQL, arbitrary URL proxy or arbitrary GraphQL route. A contract test asserts the OpenAPI document contains no such path segment.

**Default deny.** An unregistered tool, a `DRAFT`/`DISABLED`/`ERROR` tool, a caller missing a required permission, arguments that fail the schema, a tool above the org risk ceiling, or a broken integration binding are all rejected before any external call.

**P13 / P14 boundary.** P08 ships tool registration, versioning, policy, schema validation, permission enforcement, idempotency and invocation orchestration. P08 does NOT build prompt orchestration, model-provider selection, an autonomous planning loop, vendor-specific `tool_call` parsing (NXS-P13) or workflow orchestration (NXS-P14). Those callers invoke `ToolEngine` and receive a `ToolResult`; they do not reach inside it.

Consequences: adding a tool is a registry row plus an Integration Hub operation, never a code change and never a bypass of policy, permissions, schema validation or the P07 executor. The Agent Runtime can be built against a stable, auditable seam.
