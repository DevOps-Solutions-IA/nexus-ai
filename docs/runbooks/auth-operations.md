# Authentication Operations Runbook (NXS-P03)

Operational procedures for the P03 authentication foundation. The platform hardens
itself: staging and production FAIL CLOSED when signing keys are missing or unsafe,
when the key provider cannot initialize, when the runtime database role can bypass
RLS, or when an insecure rate-limit backend is selected.

## Hardened environment requirements (staging / production)

| Setting | Requirement |
| --- | --- |
| `NXS_AUTH__SIGNING_KEY` or `NXS_AUTH__SIGNING_KEY_FILE` | REQUIRED — 64-char hex Ed25519 seed (32 bytes). Generate with `openssl rand -hex 32` |
| `NXS_AUTH__ISSUER`, `NXS_AUTH__AUDIENCE` | REQUIRED, explicit values |
| `NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY` | must be false (default) |
| `NXS_AUTH__RATE_LIMIT_BACKEND` | must NOT be `local` (use `auto` or `cache`) |
| Argon2id work factors | ≥ `time_cost=3`, `memory_cost=65536` (defaults) |
| `NXS_TENANCY__HEADER_RESOLVER_ENABLED` | must be false (P02 rule, unchanged) |
| `NXS_DATABASE__VERIFY_RUNTIME_ROLE` | must be true (P02 rule, unchanged) |

A missing or unsafe value prevents STARTUP — the instance never runs insecurely.

## Local / test setup

```bash
# Explicit development keys are opt-in and refused in hardened environments.
export NXS_AUTH__ALLOW_EPHEMERAL_SIGNING_KEY=true   # fresh key per process
# or a stable dev key:
export NXS_AUTH__SIGNING_KEY="$(openssl rand -hex 32)"
export NXS_AUTH__RATE_LIMIT_BACKEND=local           # bounded in-process limiter
make up && make db-bootstrap && make migrate
```

Tests use ephemeral keys plus the local limiter via `tests/conftest.py`.

## Key rotation

1. Generate the new key: `openssl rand -hex 32 > new-signing.key`.
2. Add the CURRENT key's seed to `NXS_AUTH__VERIFICATION_KEY_SEEDS` (comma-separated
   hex seeds; the key id is derived from the public half, so this keeps verifying old
   access tokens without ever signing with the old key).
3. Deploy with `NXS_AUTH__SIGNING_KEY_FILE=<new file>`.
4. After `access_token_ttl_seconds` + `clock_skew_seconds` have elapsed (all old
   access tokens expired), remove the old seed from `VERIFICATION_KEY_SEEDS` and
   redeploy. Tokens signed by the removed key are rejected (fail closed).

## Session revocation

- `POST /api/v1/auth/logout` with a refresh token revokes the session (current or
  previously-rotated token both work; idempotent).
- Presenting an already-rotated refresh token revokes the whole session family.
- Suspending a user revokes their session server-side; revoked memberships and
  suspended Organizations block refresh and login immediately.
- Access tokens are live-state validated on every tenant request: revocation,
  suspension and logout take effect IMMEDIATELY (the `PrincipalStateValidator`
  boundary), not at token expiry.

## Rollback / recovery

- **Schema rollback:** `alembic downgrade ea956f004f7a` drops the auth tables
  (RLS policies first, then tables). Credentials and sessions are NOT recoverable
  after downgrade — only run in a controlled recovery window.
- **Bad configuration:** remove the offending `NXS_AUTH__*` value; startup fails
  closed, so a bad config can never serve traffic half-configured.
- **Compromised signing key:** rotate immediately (procedure above), add the old
  public key to the verification set ONLY if continuity matters more than immediate
  invalidation — otherwise remove it at once; revoke sessions as needed.
- **Compromised user:** suspend the user (immediate) — their sessions are revoked;
  new logins fail with the generic authentication error.

## Observability events

`auth_login_success`, `auth_refresh_rotated`, `auth_refresh_reuse_detected` (replay —
investigate), `auth_session_revoked`, `auth_authorization_denied`,
`auth_rate_limit_backend_failed` (degraded limiter — investigate the cache),
`auth_user_registered`. No passwords, hashes, tokens or key material are ever logged.
