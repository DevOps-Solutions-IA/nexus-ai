# ADR-0082: OTP delivery orchestration, failure semantics and idempotency

Status: Accepted. Part of NXS-P10 (`NXS-OTP-001`).

**Delivery path.** After the issuance transaction commits, `OtpService._deliver`
resolves the P06 customer + per-destination OTP conversation, renders the fixed content
and calls `MessagingService.send` with a deterministic messaging idempotency key
`otp-<challenge_id_hex>`. The messaging account is named in the request and must be
`ACTIVE` and on the requested channel, else `NXS_OTP_CONFIG_INVALID`.

**Failure semantics — honest, never a blind resend.**

* **Definitive failure** (provider 4xx/5xx, connection error, `NXS_MSG_RATE_LIMITED`):
  the challenge is REVOKED in a follow-up transaction, `otp.challenge.delivery_failed`
  is emitted with the underlying `NXS_MSG_*` code, and `NXS_OTP_DELIVERY_FAILED` is
  raised. A code that was never sent is never left usable.
* **Ambiguous timeout** (`NXS_MSG_TIMEOUT`): delivery may or may not have happened. The
  challenge stays `ACTIVE` (a code that did arrive is still enterable), the result
  reports `delivery = UNCONFIRMED`, and the OTP is **never** silently regenerated or
  resent. A caller that needs another code issues one explicitly (subject to cooldown).
* **Success**: `delivery_message_id` is linked; the result reports `delivery = SENT` and
  carries only masked metadata.

**Issuance idempotency.** `IssueOtpRequest.idempotency_key` is stored on the challenge
with `UNIQUE (organization_id, idempotency_key)` plus a `request_fingerprint` over
`(purpose, channel, destination, messaging_account_id)` — the code is **not** in the
fingerprint. Same key + same fingerprint replays the stored safe result with
`replayed = true`, `delivery = SKIPPED` and **no** second send. Same key + a different
fingerprint is `NXS_OTP_IDEMPOTENCY_CONFLICT`. A concurrent same-key race collapses on
the unique constraint and replays the winner. Verification is naturally replay-safe
through the terminal challenge state (ADR-0080).

**Cleanup seam.** `OtpService.purge_expired` / `OtpChallengeRepository.purge_terminal_before`
delete terminal challenges older than a retention window (min 1 h); ACTIVE challenges are
never purged. NXS-P15 owns scheduling — P10 only provides the callable seam.

Consequences: the issuance API tells the truth about delivery, an ambiguous network
event never causes duplicate uncontrolled sends, and a retried request with the same key
costs the user nothing.
