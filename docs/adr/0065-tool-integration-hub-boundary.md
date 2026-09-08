# ADR-0065: Tool Engine ↔ Integration Hub boundary

Status: Accepted. Context: NXS-TOOL-001 requires that all external API execution goes through the governed Integration Hub (NXS-P07) and that the LLM never receives plaintext secrets, raw credentials, unrestricted URLs or methods, arbitrary headers, arbitrary GraphQL documents, direct database handles or provider SDK credentials. ADR-0054 fixed the Hub as the single outbound route; this record fixes how the Tool Engine sits on top of it.

Decision:

**The Tool Engine never executes anything itself.** Step 12 of `ToolEngine.invoke` calls `IntegrationHubService.execute(organization_id, ExecutionRequest(integration_id=tool.binding.integration_id, operation_key=tool.binding.operation_key, input=merged_arguments, idempotency_key=<derived>, correlation_id=...))` and nothing else. It holds no HTTP client, no URL, no header logic, no GraphQL support and no credential access. HTTP / OpenAPI / GraphQL / CRM / ERP / calendar execution stays entirely in P07.

**The model chooses a tool, not an operation.** The caller supplies `tool_key`; the engine reads `integration_id` and `operation_key` from the tenant-owned binding. The model never sees, sets or tampers with `operation_key` — a contract test and adversarial security tests assert that injecting `operation_key`, `url`, `method`, `headers` or `query` into `arguments` is rejected by the closed input schema as `NXS_TOOL_ARGS_INVALID`.

**Static arguments win.** `merge_arguments(caller_arguments, static_arguments)` deep-merges the tool's fixed `static_arguments` *over* the caller's arguments, so an operator can pin a path prefix, a filter or a constant and the model cannot override it. The merge result is what gets fingerprinted and what the Hub receives.

**Secrets never transit the engine.** Credentials live in the P07 vault seam and are applied inside the Hub executor. The `ToolResult`, the `tool_execution_records` row and every `tools.*` event carry only the normalized output and status metadata — no request body, header, base URL or secret. A security test drives a tool whose integration has an auth profile and asserts the secret reaches the upstream server but never appears in the result, the record or the events.

**Downstream failures are translated, not leaked.** `IntegrationHubService.execute` raises `NxsError` on failure; `map_downstream_error` converts it to the tool taxonomy (ADR-0066) and stores the original `downstream_code` for audit — the raw upstream body is not surfaced to the caller.

Consequences: a new provider is a Hub configuration; the Tool Engine gains it for free with no code change and no new bypass. The P07/P08 split is enforced by the absence of any executor in `src/nexus_ai/tools/`.
