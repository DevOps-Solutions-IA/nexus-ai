# ADR-0038: Global user identity plane

Status: Accepted. Context: P03 needs a canonical user identity without weakening the P02 tenant boundary. Decision: `users` and `user_credentials` are consciously classified GLOBAL — no `organization_id`, no RLS, and the runtime role holds only SELECT/INSERT/UPDATE. Tenant association NEVER derives from these tables: it flows exclusively through tenant-scoped `memberships` and `role_assignments` under forced RLS. Identity data (id UUIDv7, email, lifecycle status, verified flag) is immutable in the identity sense: `user_id` is server-generated and never client-supplied; email is trimmed, lowercased, NFC-normalized and uniquely indexed; user status is ACTIVE/SUSPENDED with no runtime DELETE. Consequences: the identity plane is queryable for authentication without tenant scope, but a global user row grants nothing by itself — access to any Organization always requires an ACTIVE membership resolved through tenant RLS, and cross-tenant attacks are proven impossible by `tests/integration/test_auth_rls.py`.

## Table classification

| Table | Class | Why |
| --- | --- | --- |
| `users` | GLOBAL | Identity plane; login precedes any Organization scope |
| `user_credentials` | GLOBAL | Per-user password state; latest version verifies, history kept |
| `memberships` | TENANT-OWNED | The authoritative user↔Organization boundary (forced RLS) |
| `role_assignments` | TENANT-OWNED | Authorization is per-Organization by definition |
| `refresh_sessions` | TENANT-OWNED | Sessions are minted and consumed within one Organization |
| `roles`, `permissions`, `role_permissions` | GLOBAL | Platform-wide catalogs; assignments are the tenant-scoped part |
