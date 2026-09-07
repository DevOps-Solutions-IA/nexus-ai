# Organization Provisioning and Dashboard Schema (NXS-P05)

The permanent enterprise Organization provisioning foundation. The canonical tenant
remains `organization_id`; P02/P03 semantics are reused and extended, never duplicated.

## Provisioning sequence

```
POST /api/v1/organizations  (Bearer + live session + platform organization:create)
  1. validate onboarding (Pydantic, extra=forbid, P02 slug rules, IANA tz, BCP-47 locale)
  2. validate owner (must EXIST and be ACTIVE — P05 never creates users)
  3. Phase 1  claim: provisioning_requests PENDING (key-hash unique, fingerprint,
              validated payload snapshot — never the raw key, never verbatim input)
  4. Phase 2  baseline (ONE transaction + savepoint):
              org core (PROVISIONING) → owner membership + org_owner (FIXED role)
              → settings (locale) → dashboard baseline (revision 1)
              → organizations.provisioned outbox intent → org ACTIVE
  5. Phase 3  finalize: provisioning_requests COMPLETED
```

Deterministic business failures → terminal FAILED row + stable error code. Any
infrastructure failure → full rollback (nothing visible). NATS is never touched on
the request path — the P04 relay publishes the outbox row later.

## State model (two lifecycles, never conflated)

| Concern | State | Owner |
| --- | --- | --- |
| Organization operational lifecycle | `organizations.status` (PROVISIONING/ACTIVE/SUSPENDED/ARCHIVED) | P02 |
| Provisioning workflow lifecycle | `provisioning_requests.status` (PENDING/COMPLETED/FAILED) | P05 |

## Idempotency

- Same key + same fingerprint → the original result (replay).
- Same key + different fingerprint → 409 `NXS_PROV_IDEMPOTENCY_CONFLICT`.
- Same slug, different keys → exactly one winner; losers 409 `NXS_ORG_CONFLICT`.
- FAILED replay → 409 `NXS_PROV_FAILED` with the recorded sanitized error code.
- PENDING + no org + fresh → 409 `NXS_PROV_IN_PROGRESS` (retryable).
- PENDING + no org + stale (> 60 s) → deterministic resume under the original claim.
- PENDING + org exists → finalize (crash between Phase 2 and 3).
- Exactly-once is NOT claimed; duplicate safety comes from database constraints.

## Platform capability

`organization:create` is granted through `platform_grants` (user-scoped) — no
Organization role ever holds it, and Organization roles never leak into platform
operations. The caller must hold an authenticated live session (P03 state validation
runs before business logic); platform operators use their home Organization's
session.

## Permissions added (P05 migration delta)

`organization:create` (platform), `organization:provision:read`,
`organization:settings:read`, `dashboard:read` — granted to org_owner/org_admin/
org_member (the three org-scoped ones) via the P05 catalog seed.

## API surface

| Endpoint | Auth | Notes |
| --- | --- | --- |
| `POST /api/v1/organizations` | live session + organization:create | idempotent; 201 |
| `GET /api/v1/organizations/current/provisioning` | organization:provision:read | tenant-safe |
| `GET /api/v1/organizations/current/settings` | organization:settings:read | tenant RLS |
| `GET /api/v1/organizations/current/dashboard-schema` | dashboard:read | permission-filtered, revalidated |

No arbitrary-id cross-tenant reads, no event-publish API, no dashboard-execution API.

## Dashboard schema

`docs/adr/0051-dashboard-schema-architecture.md`. Baseline widgets:
organization.profile, organization.provisioning_status, organization.settings_summary.
Stored snapshots revalidate against the registry (unknown widget / unregistered data
source / permission tampering / unsupported version → fail closed) and are projected
to the viewer's permissions.

## Failure matrix

`docs/runbooks/provisioning-failure-matrix.md` — expected state, retryability,
API behavior, DB state, event state and operator action for every case.

## Non-scope

No frontend, no CRM/credentials/channels/telephony/agent/workflow/campaign/Sentinel
content, no billing, no cell assignment, no email delivery. The owner user must
already exist (documented precondition).
