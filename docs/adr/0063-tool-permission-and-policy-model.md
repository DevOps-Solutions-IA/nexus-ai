# ADR-0063: Tool permission and policy model

Status: Accepted. Context: NXS-TOOL-001 requires server-side permission enforcement — the model's claims about what it may do are never trusted — plus an organization/tool policy layer and mandatory tenant isolation that fails closed.

Decision:

**Permissions are server-resolved, never caller-supplied.** `ToolPermissionGuard.require(tenant, principal, required)` resolves the caller's granted permissions through `AuthorizationService.permissions_for(tenant, principal.user_id)` (the P03 RBAC catalog) inside the tenant transaction, and rejects with `NXS_TOOL_PERMISSION_DENIED` (carrying a `missing_permissions` extension) if any required permission is absent. Nothing in `ToolInvocation` influences this.

**A tool's required permissions are validated and folded at registration.** `normalise_required_permissions` checks every declared permission against the frozen `PermissionKey` catalog (unknown → `NXS_TOOL_CONFIG_INVALID`) and always folds in the implied floor `integration:execute` + `tool:invoke`. So invoking any tool requires at minimum that the caller may invoke tools and may drive the Integration Hub, plus whatever the tool additionally declares.

**New RBAC keys.** `tool:read / tool:create / tool:update / tool:disable / tool:invoke`, with deterministic ids seeded by the P08 migration. `owner` and `admin` hold all five; `org_member` holds `tool:read` + `tool:invoke` only — a member invokes governed tools but never registers, edits, enables or disables one. `ROLE_PERMISSIONS` stays frozen at P03; these grants are migration seed deltas.

**Policy layer.** After authorization, `_check_policy` enforces the organization risk ceiling (`settings.tools.max_risk_class`, default `HIGH`) against the tool's `risk_class` — a tool above the ceiling is rejected with `NXS_TOOL_POLICY_DENIED` even if it is `ACTIVE` and the caller is permitted. There is no autonomous approval path and no fail-open configuration: an unset or unrecognised policy value is a rejection, not a pass.

**Tenant isolation fails closed.** `organization_id` is always `principal.organization_id`. Registry reads and writes run in `Database.tenant_transaction(org_id)` under forced RLS, so a cross-organization `tool_key` or `tool_id` simply does not resolve (`NXS_TOOL_NOT_FOUND`). A forged tenant is impossible because the contract has no organization field.

Consequences: the permission surface is auditable from the catalog and the migration; a member cannot escalate by asserting a role; the risk ceiling gives an operator one dial to bound what the AI plane can do per organization.
