# ADR-0081: OTP throttling, resend / reissue policy, purposes and templates

Status: Accepted. Part of NXS-P10 (`NXS-OTP-001`).

**One live code per subject / purpose.** A PARTIAL UNIQUE INDEX
`uq_otp_challenges_one_active (organization_id, destination_fingerprint, purpose)
WHERE status = 'ACTIVE'` makes "at most one ACTIVE challenge" a database invariant. A new
successful issuance for the same `(organization, destination, purpose)` REVOKES the prior
ACTIVE challenge (`otp.challenge.revoked`, reason `superseded_by_new_issuance`) in the
same transaction as the insert. A concurrent double-issue resolves deterministically:
one wins, the other hits the partial-unique violation and gets `NXS_OTP_RESEND_TOO_SOON`.

**Resend cooldown.** Every challenge carries `resend_after = issued_at +
NXS_OTP__RESEND_COOLDOWN_SECONDS` (default 60, bounded 15–900). A reissue for a subject
whose current ACTIVE challenge is still inside its cooldown is rejected with
`NXS_OTP_RESEND_TOO_SOON` before any code is generated.

**Durable issuance throttle.** `count_issued_since(destination_fingerprint, purpose,
now − NXS_OTP__ISSUE_WINDOW_SECONDS)` counts prior issuances (any status) in the window;
`>= NXS_OTP__MAX_ISSUES_PER_WINDOW` (default 5 per 3600 s) raises `NXS_OTP_RATE_LIMITED`
and emits `otp.challenge.rate_limited` in a standalone transaction (so the audit survives
the rejected issuance's rollback). The counter is a table query, not a process-local
counter, so it holds across instances.

**Purposes.** `nexus_ai.otp.purposes` is an allow-listed registry, not a free-form
string: `GENERIC_VERIFICATION` (SMS or Email), `VERIFY_EMAIL` (Email), `VERIFY_PHONE`
(SMS). Each declares its permitted channels; an unknown purpose or a purpose / channel
mismatch is `NXS_OTP_PURPOSE_INVALID`. The purpose is part of the keyed-verifier context
(ADR-0079), so a code cannot be replayed across purposes. Only purposes the backend needs
today are registered — a downstream product flow (login, password reset, …) would
register its own when it exists.

**Templates.** `nexus_ai.otp.templates.render_message` returns fixed content: no
caller-supplied template, no HTML, no user-controlled header, no template-expression
evaluation. Exactly two subsystem-produced values are interpolated — the digits and the
whole-minute TTL.

Consequences: an attacker cannot farm unlimited valid codes for a destination, cannot
keep multiple codes alive, and cannot smuggle content or an expression through the
template.
