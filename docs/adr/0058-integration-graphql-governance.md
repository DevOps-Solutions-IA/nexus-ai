# ADR-0058: GraphQL governance

Status: Accepted. Context: NXS-INT-001 supports GraphQL integrations, but a GraphQL endpoint accepts arbitrary queries by design — depth, aliasing and introspection are all abuse vectors, and Nexus must never let a caller (or the LLM via P08) send a free-form document. Decision:

**Fixed registered documents only.** A `GraphQLOperationSpec` stores one immutable `document`. The caller of an execution supplies `{"variables": {...}}` and nothing else — a raw `query`, a `mutation`, extra top-level keys or an arbitrary document are all rejected with `NXS_INT_OPERATION_INPUT_INVALID`.

**Registration-time structural analysis.** Nexus embeds no GraphQL engine; `analyse_document` strips comments and string literals, then:

- rejects introspection — any `__schema` / `__type` reference;
- rejects `subscription`;
- detects `mutation` and refuses it unless the operation sets `allow_mutation`;
- computes selection-set nesting from balanced braces and rejects a document whose depth exceeds `max_depth` (bounded 1–20, further clamped by `NXS_INTEGRATIONS__OPENAPI_MAX_DEPTH`);
- rejects unbalanced braces.

**Execution.** Variables are validated against the operation's `variables_schema` (JSON Schema), the request is a single bounded POST through the governed executor (same SSRF, timeout and size limits as REST), and the response is bounded and validated: a non-200, a non-object body, or a body carrying a non-empty `errors` array is `NXS_INT_RESPONSE_INVALID`; `data` is checked against an optional response schema. GraphQL introspection is therefore disabled end to end — at registration (document scan) and at runtime (no arbitrary document can be sent).

Consequences: a GraphQL integration is as constrained as a REST integration — a finite set of reviewed operations with typed inputs and bounded, validated outputs. Adding a new query is a registry change an operator makes, not something a caller can do.
