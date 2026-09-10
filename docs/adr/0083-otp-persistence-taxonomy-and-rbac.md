# ADR-0083: OTP persistence, error taxonomy, RBAC and threat model

Status: Accepted. Part of NXS-P10 (`NXS-OTP-001`).

**Persistence — one tenant-owned table.** `otp_challenges` (migration `d2e3f4a5b6c7`,
revises `c1d2e3f4a5b6`) has `organization_id` NOT NULL, RLS **ENABLED + FORCED**, the
`nxs_tenant_isolation` policy bound to `current_setting('nxs.organization_id')`, and
grants `SELECT, INSERT, UPDATE, DELETE` to `nexus_runtime` (DELETE for the cleanup seam).
No separate rate-limit table is created — the throttle is a windowed query over this one
table.

**Composite tenant-aware FKs.** `(organization_id, messaging_account_id) →
messaging_accounts(organization_id, id)` `ON DELETE RESTRICT`; `(organization_id,
delivery_message_id) → messaging_messages(organization_id, id)` `ON DELETE SET NULL
(delivery_message_id)` (the PostgreSQL 15+ column-specific form, ADR-0077, so a future
message purge NULLs only the pointer and leaves the NOT NULL `organization_id`). A forged
cross-tenant `messaging_account_id` is refused at the database.

**DB-enforced invariants.** `CHECK` constraints pin the status and channel enums, the
subject type, `attempts >= 0`, `max_attempts BETWEEN 1 AND 10`, `attempts <= max_attempts`
and `expires_at > issued_at`. `UNIQUE (organization_id, idempotency_key)` and the partial
unique index `uq_otp_challenges_one_active` (ADR-0081) enforce idempotency and the
one-active-challenge policy. The tenant schema guard passes with zero violations.

**Error taxonomy.** RFC 9457 `NXS_OTP_*`: `INVALID` (401 — also covers revoked /
unknown-but-plausible so verification is not an enumeration oracle), `EXPIRED` (410),
`ALREADY_USED` (409), `LOCKED` (423), `ATTEMPTS`… folded into `LOCKED`, `RATE_LIMITED`
(429, retryable), `RESEND_TOO_SOON` (429, retryable), `DELIVERY_FAILED` (502),
`CHALLENGE_NOT_FOUND` (404), `PURPOSE_INVALID` (422), `CONFIG_INVALID` (422),
`IDEMPOTENCY_CONFLICT` (409). No error carries the code, the hash, the pepper or the full
destination.

**RBAC.** `otp:read` / `otp:issue` / `otp:verify`, deterministic ids
`b2000000-…-000000000023..25`, seeded as a migration delta: owner + admin get all three;
`org_member` gets all three (an internal app surface, not a public abuse surface).
`ROLE_PERMISSIONS` (frozen at P03) is unchanged.

**Threat model / auth stance.** Every OTP route is authenticated and permission-checked
(option A: authenticated internal API). An unauthenticated "login OTP" flow is
deliberately NOT exposed — no consumer exists, and a public unauthenticated issuance
endpoint would be an abuse surface. When a real login / password-reset flow is built it
registers its own purpose and calls `OtpService`; P13 and a frontend are out of scope.
Enumeration is resisted by: a uniform `NXS_OTP_INVALID` for wrong-code / revoked /
plausible-unknown, masked destinations everywhere, and no "does this destination exist"
signal in issuance (issuance always succeeds structurally or fails on config / rate).

Consequences: the OTP store is a PostgreSQL-enforced tenant boundary with the safety
rules in `CHECK` / `UNIQUE` / FK constraints, not application code; the API is a governed
internal surface with a documented, conservative auth posture.
