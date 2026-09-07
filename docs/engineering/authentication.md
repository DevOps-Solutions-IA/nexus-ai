# Security and Authentication (NXS-P03)

The permanent authentication and authorization foundation. P03 completes the P02 seam
(ADR-0036): the production `TenantContextResolver` now derives `organization_id` only
from a verified access token minted after server-side membership verification.

## Identity plane

- **Users** (global, no RLS): UUIDv7 id (server-generated, immutable), normalized
  unique email, `email_verified`, ACTIVE/SUSPENDED status, versioned row.
- **Credentials** (global): Argon2id only, per-password salt, versioned history
  (`credential_version`), bounded inputs (12–128 code points, no control chars).
- Email normalization: trim → lowercase → NFC → conservative ASCII validation.
  Passwords are never normalized — the byte sequence is opaque.

## Membership boundary

`memberships` is tenant-owned with forced RLS and one row per (organization, user).
Status transitions mutate the single row — duplicate-active ambiguity is structurally
impossible. A second USING-only policy (`nxs_principal_self`) exposes exactly the
caller's own rows when the transaction binds `nxs.principal_id` without org scope
(the login enumeration path). No WITH CHECK on that policy: nobody creates their own
membership outside an Organization-scoped write.

## Token and session lifecycle

```
login ──► verify credentials (global) ──► enumerate own ACTIVE memberships
      ──► select Organization (hint honored only if it is the caller's own)
      ──► verify Organization ACTIVE through its tenant scope
      ──► insert refresh session (tenant RLS WITH CHECK) ──► mint access token (15 min)

refresh ─► parse org prefix ─► tenant scope ─► row by token_hash
        ─► revoked/expired → 401 │ previous-hash match → REVOKE family, 401
        ─► user ACTIVE + membership ACTIVE ─► conditional rotation
        ─► mint fresh pair (same session id, new refresh token)

logout ─► revoke by current OR previous hash (idempotent, 204 always)
```

Rotation is a conditional UPDATE (`token_hash → new`, `previous_token_hash → old`):
concurrent refreshes yield exactly one winner; a loser is replay and revokes the
family. Revocations always commit before any error is raised.

## Key management

`SigningKeyProvider` protocol → `LocalEd25519KeyProvider` (seeds from config or file).
`kid = sha256(public)[:16]`. Rotation: add the old public key to
`verification_key_seeds`, deploy, remove after the access-TTL window. Private material
is repr-redacted and never logged or returned. KMS/HSM is a future provider behind the
same protocol (seam only — not implemented).

## RBAC

Global catalogs (seeded deterministically by the migration):

| Role | Permissions |
| --- | --- |
| `org_owner` | organization:read, organization:write, membership:manage, role:assign, auth:session:read |
| `org_admin` | all of the above except role:assign |
| `org_member` | organization:read, auth:session:read |

Assignments are tenant-owned RLS rows. `AuthorizationService.require` is
deny-by-default and per-Organization. The first membership+owner assignment is created
by the P05 provisioner through `MembershipService.create(actor=None)` — the internal
bootstrap seam, deliberately not an HTTP endpoint.

## API surface (minimal)

| Endpoint | Auth | Behavior |
| --- | --- | --- |
| `POST /api/v1/auth/login` | none | tokens + identity; 401 `NXS_AUTH_CREDENTIALS_REJECTED` (generic); 400 `NXS_AUTH_MULTIPLE_ORGS`; 429 throttled |
| `POST /api/v1/auth/refresh` | none | rotation; replay revokes family |
| `POST /api/v1/auth/logout` | none | idempotent revocation, 204 |
| `GET /api/v1/auth/me` | Bearer | identity + session state |
| `GET /api/v1/auth/memberships` | Bearer | the caller's own memberships only |

No registration endpoint, no user-admin CRUD, no OAuth/MFA claims.

## Configuration

All `NXS_AUTH__*`. Hardened (staging/production) fail-closed rules: signing key or key
file REQUIRED (ephemeral refused), explicit issuer+audience REQUIRED, Argon2id ≥
t=3/m=64 MiB, rate-limit backend ≠ `local`. Local/test may use
`NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY=true` explicitly.

## Database roles

`nexus_migration` owns the schema (migration `36741ae62327`). `nexus_runtime` gets
SELECT/INSERT/UPDATE on the auth tables (never DELETE, never DDL); tenant-owned auth
tables carry forced RLS and are covered by `scripts.nxs_schema_guard`.

## Structured security events

`auth_login_success`, `auth_refresh_rotated`, `auth_refresh_reuse_detected`,
`auth_session_revoked`, `auth_authorization_denied`, `auth_rate_limit_backend_failed`,
`auth_user_registered`. Never: passwords, hashes, tokens, key material. P22 owns the
full audit ledger.

## Related

- Threat model + test matrix: `docs/adr/0046-auth-threat-model.md`
- Operations (key rotation, revocation, rollback): `docs/runbooks/auth-operations.md`
- ADRs 0038–0045
