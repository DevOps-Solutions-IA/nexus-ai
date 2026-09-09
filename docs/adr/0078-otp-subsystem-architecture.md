# ADR-0078: OTP Services — subsystem architecture and the P10 ↔ P09 boundary

Status: Accepted. Establishes NXS-P10 (`NXS-OTP-001`): a secure, deterministic,
tenant-isolated one-time-code subsystem for generic authentication / verification flows.

**Scope.** P10 owns OTP issuance, cryptographically secure code generation, keyed
hashing, expiration, verification, single-use semantics, replay resistance, attempt
limits, resend / reissue policy, durable throttling, lockout / cooldown, audit events,
delivery *orchestration*, a deterministic error taxonomy, tenant isolation and
concurrency safety. P10 does NOT own generic SMS / Email / WhatsApp transport (NXS-P09),
the AI Agent Runtime (P13), Workflows (P14), the Scheduler (P15), Campaigns (P16) or
Human-Agent Operations (P17), and ships no frontend.

**Package layout.** `nexus_ai.otp` — `entities` (domain values + strict API contracts),
`errors` (`NXS_OTP_*`), `codes` (CSPRNG + keyed verifier + masking), `purposes`
(allow-listed registry), `templates` (fixed content), `events` (P04 payloads),
`service` (`OtpService`). Persistence is `nexus_ai.domain.otp` — `OtpChallengeRecord`
(one tenant-owned table, forced RLS) + `OtpChallengeRepository`.

**P10 ↔ P09 boundary.** P10 delivers *only* through `MessagingService.send` over a
configured NXS-P09 messaging account named in the request. It never opens a socket,
never speaks a provider protocol, never holds a provider credential and adds no per-send
URL / method / header surface. P09 stays a generic messaging subsystem — it has no
import of `nexus_ai.otp` and no OTP-specific method (contract-guarded). The one-time-code
never leaves this subsystem except inside the delivered message body; for tests it is
recovered from the fake transport, never from a production API.

**P10 ↔ P06 boundary.** The Customer / Identity / Conversation / timeline model is
NXS-P06's. `OtpService` resolves a customer and a per-destination OTP conversation
(`external_thread_id = "otp:<destination_fingerprint>"`) through the P06 services and
attaches the delivery message to it; it never re-implements that model.

**Authentication model.** Every OTP route is an authenticated internal API guarded by
`otp:issue` / `otp:verify` / `otp:read`. No unauthenticated "login" flow is exposed —
no such consumer exists yet (see ADR-0083). NXS-P10 remains the generic mechanism a
later phase would build a product flow on; P11–P17 stay `PLANNED`.

Consequences: OTP is a self-contained capability layered cleanly on P06 + P09; a future
consumer registers a purpose and calls `OtpService`, and nothing about P09 changes.
