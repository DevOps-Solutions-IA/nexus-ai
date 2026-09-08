# ADR-0057: Registered-operation execution, OpenAPI ingestion and inbound webhooks

Status: Accepted. Context: NXS-INT-001 must let tenants describe external operations without ever accepting an arbitrary request, must be able to import an OpenAPI document without auto-exposing every endpoint, and must accept inbound webhooks with trusted tenant mapping. Decision:

**Registered REST operations.** `RestOperationSpec` declares `method`, a path template, allow-listed `path_params` / `query_params` / `header_params` (each a `ParamSpec` with `required` / regex `pattern` / `max_length` / fixed `constant`), an optional request-body JSON Schema, `success_status`, an optional response JSON Schema, and a `RetryClass`. At execution `RestInvocationBuilder`:

- parses the caller input into `{path_params, query_params, headers, body}` and rejects any key the operation did not declare;
- validates every value against its `ParamSpec`, percent-encodes path values into the template, builds the query from validated values plus constants;
- validates the body against the schema and bounds its size;
- refuses any reserved header (`Authorization`, `Host`, `Content-Length`, `Cookie`, …) and never lets a caller override the auth layer's headers;
- `DELETE` is available only when the operation's `method` is `DELETE` (there is no implicit delete).

The operation's effective destination (`base_url + path`) is SSRF-validated at registration.

**GraphQL** (`GraphQLOperationSpec`). Executed ONLY as a fixed, pre-registered document; the caller supplies variables, never a query string. At registration a bounded brace-depth scanner (Nexus embeds no GraphQL engine) enforces: selection nesting within `max_depth`; introspection (`__schema` / `__type`) refused; a `mutation` / `subscription` refused unless the operation explicitly enables mutations. At execution the variables are schema-validated and the request is a single bounded POST; a GraphQL response carrying `errors` is `NXS_INT_RESPONSE_INVALID`.

**OpenAPI ingestion** (`OpenApiIngestor`). `parse (JSON, size-bounded) → validate (3.x shape, bounded nesting, bounded operation count) → sanitize (reject non-local `$ref`, callbacks, `webhooks`, unsafe server URLs) → extract allow-listed candidate operations → return them for HUMAN selection`. Import performs no network fetch and registers nothing; only the operations an operator explicitly selects become normal `RestOperationSpec` rows.

**Inbound webhook foundation.** An endpoint is identified by an unguessable `public_token` whose prefix carries the base64 organization id, so the receive path binds the tenant scope BEFORE any RLS-scoped lookup — the payload is never trusted for tenant mapping. Each request is size-bounded to the endpoint limit; signature-verified when a scheme is configured (HMAC-SHA256, constant-time compare, timestamp tolerance window, secret from the vault, secret never logged) — unsigned is never treated as verified; replay-checked against a durable per-endpoint dedup key (`webhook_receipts`, `UNIQUE(org, endpoint, external_id)`); normalised to an ID-only event and enqueued through the P04 transactional outbox in the same transaction as the receipt. This is NOT a WhatsApp / channel-provider implementation (NXS-P09+) and it never forwards the raw payload. Outbound webhooks are deferred (no justified P07 use).

**Adapters.** `CRMAdapter` / `ERPAdapter` / `CalendarAdapter` are Protocols; `GenericRestAdapter` satisfies all three by mapping each method to a conventional `operation_key` and calling `IntegrationHubService.execute`. An adapter adds no transport, no auth and no URL — it cannot bypass the governed path.

Consequences: the surface a caller (and the future Tool Engine) can reach is exactly the set of registered operations; importing a large public API yields a reviewed shortlist, not hundreds of live endpoints; a webhook is attributed to a tenant by configuration, verified cryptographically and processed exactly once per unique delivery.
