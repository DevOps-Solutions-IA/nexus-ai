# ADR-0054: Integration Hub architecture

Status: Accepted. Context: NXS-INT-001 requires a permanent, provider-neutral way for Nexus to communicate with external systems (REST, OpenAPI, GraphQL, CRM, ERP, calendar, inbound webhooks) that the future AI Tool Engine (NXS-P08) can build on without the LLM ever touching a raw API or a plaintext secret. Decision:

**The governed path.** Every outbound call follows exactly one route: `Caller → Integration Registry → Integration Policy (SSRF + config) → Credential Reference (vault seam) → Integration Executor (governed HTTP / GraphQL) → Response validation → normalized IntegrationResult`. `IntegrationHubService.execute` is the only entry point; there is no arbitrary-URL proxy, no raw-header forwarding, no arbitrary GraphQL executor, and no `POST /api/v1/http/request`.

**Operation-based execution.** A caller supplies `integration_id + operation_key + validated input` — never a URL, method, header set, query string or GraphQL document. Those live only in the tenant-owned, config-versioned registry (`integrations`, `integration_operations`). Every change to a base URL, auth profile, destination rule or operation spec increments `config_revision`, and every execution record is attributed to the revision it ran against.

**Tenant isolation is a PostgreSQL boundary.** All seven Hub tables are `TenantOwnedMixin` with forced RLS (`ENABLE` + `FORCE`, `USING` + `WITH CHECK`). Rows that reference another Hub table use composite tenant-aware FKs `(organization_id, <parent_id>) → (organization_id, id)` (ADR-0052 pattern) so the database itself refuses a cross-tenant attachment. `scripts.nxs_schema_guard` fails CI on an unsafe Hub table.

**Lifecycle.** An integration is `DRAFT → ACTIVE → DISABLED` (and `ERROR` for a future health signal). Execution requires `ACTIVE`. RBAC: `integration:read/create/update/disable`, `integration:operation:manage`, `integration:credential:manage`, `integration:test`, `integration:execute`, `integration:webhook:manage`. owner/admin hold all; `org_member` holds `integration:read` + `integration:execute` only (a member invokes configured integrations but never manages one).

**Events.** `integrations.created / updated / disabled / operation.created / execution.failed / webhook.received` — registered, versioned, strict, ID-only payloads, enqueued through the P04 transactional outbox in the same transaction as the mutation. No external body, header, secret or base URL ever reaches the event bus.

**P08 boundary.** P07 ships the registry, the vault seam, the HTTP/GraphQL executors, the OpenAPI import pipeline, the adapter interfaces, the destination policy and response validation. P07 does NOT build LLM tool definitions, a tool registry, AI tool selection, tool-permission logic, input mapping from a model, tool result flow, or `tool_call` parsing — that is NXS-P08 / NXS-TOOL-001, which stays `PLANNED`.

Consequences: later channel/provider phases (P09+) and the Tool Engine (P08) plug into this path; a provider suite is a `GenericRestAdapter` configuration plus registered operations, never a bypass of policy, credentials, SSRF, the executor or the schemas.
