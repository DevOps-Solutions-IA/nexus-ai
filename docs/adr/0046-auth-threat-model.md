# ADR-0046: P03 authentication threat model

Status: Accepted. Context: P03's security claims rest on an explicit, tested threat model. Decision: the P02 tenant threats carry over unchanged (RLS is not weakened anywhere); P03 adds the identity/session threats below. Every row names the test file that proves the defense.

| Threat | Defense | Evidence |
| --- | --- | --- |
| Token forgery: wrong key/signature, altered sub/org, alg=none, HS256 confusion with the public key as secret | EdDSA-only allowlist, explicit typ check, kid resolution | `tests/unit/test_auth_tokens.py` |
| Replayed/already-rotated refresh token | previous-token-hash match → family revocation (committed before the error) | `tests/integration/test_auth_integration.py`, concurrency suite |
| Credential stuffing / enumeration | identical generic 401 for unknown email, bad password, foreign-org hint; dummy Argon2id verify for unknown emails | `tests/integration/test_auth_integration.py` |
| Disabled user / suspended or revoked membership / suspended Organization | rejected at login AND refresh; suspension revokes sessions | identity-attack integration tests |
| Cross-tenant membership, assignment, or session access via raw SQL | forced RLS on all three tables; runtime role NOSUPERUSER/NOBYPASSRLS, no DELETE, no DDL | `tests/integration/test_auth_rls.py` |
| Permission misuse: escalation, forged role key, inactive assignment, org-A role reused in org B | deny-by-default AuthorizationService over tenant-scoped assignments | `tests/integration/test_auth_rbac.py` |
| Secret leakage: password, refresh token, signing key, DSN, raw crypto errors | repr-redacted key material, hash-only session storage, problem-details-only responses, redacting logging | leakage tests (unit + integration) |
| Brute-force login | bounded fixed-window limiter, cache-backed in hardened environments | ratelimit unit + integration tests |
| Concurrent refresh / logout races | conditional in-place rotation; exactly one winner; replay revokes the family deterministically | `tests/concurrency/test_auth_concurrency.py` |
| ContextVar/pool leakage across organizations | per-request token-derived context; transaction-local GUCs | multi-tenant concurrency tests |

Residual risk (accepted, documented): access tokens stay cryptographically valid until expiry after revocation (bounded by the 15-minute default TTL); rate limiting in local/test uses the in-process backend; the runbook (`docs/runbooks/auth-operations.md`) prescribes the hardened configuration for staging/production.
