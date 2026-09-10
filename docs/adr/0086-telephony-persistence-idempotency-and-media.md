# ADR-0086: Telephony persistence, idempotency, failure semantics and the media-session boundary

Status: Accepted. Part of NXS-P11 (`NXS-TEL-001`).

**Tables** (migration `e3f4a5b6c7d8`, revises `d2e3f4a5b6c7`; the corrective migration
`f4a5b6c7d8e9` adds the nullable `telephony_calls.request_fingerprint` column) — 6, all
TENANT-OWNED with forced RLS, `nexus_runtime` as the non-bypass role, tenant schema
guard clean:

| Table | Purpose |
|---|---|
| `telephony_accounts` | provider accounts (asterisk / fake). `(provider, external_account_id)` and `webhook_token` GLOBALLY unique |
| `telephony_phone_numbers` | Organization-owned, provider-verified numbers. `e164` GLOBALLY unique — a number belongs to exactly one Organization (caller-ID governance + inbound tenancy) |
| `telephony_calls` | the normalized call + state machine. Composite tenant-aware FKs `(organization_id, account_id) → telephony_accounts` (RESTRICT) and `(organization_id, from_number_id) → telephony_phone_numbers` (`ON DELETE SET NULL (from_number_id)`, ADR-0077). `provider_call_id` unique per `(organization_id, account_id)`; `idempotency_key` unique per `organization_id` |
| `telephony_call_events` | the durable per-provider-event idempotency + audit log — unique per `(organization_id, account_id, provider_event_id)` |
| `telephony_media_sessions` | the media-session foundation; composite FK to `telephony_calls`, `ON DELETE CASCADE` |
| `telephony_secrets` | Fernet ciphertext only (same seam as the NXS-P07 vault; a dedicated table isolates the credential plane) |

`CallLeg` / `Trunk` / `SipEndpoint` are domain models, not tables: legs are a bounded
JSON array on `telephony_calls`, a trunk / SIP endpoint is bounded account
configuration. NXS-P12 promotes legs to their own table only if it needs per-leg media
rows (this ADR is the reference).

**Call-event idempotency.** A provider callback is claimed by inserting a
`telephony_call_events` row for `(organization, account, provider_event_id)`; a
unique-violation → deterministic `replayed`. The claim + the state fold + the media fold
+ the P04 outbox event commit **atomically** in one tenant transaction — a crash rolls
the claim back and the provider retry reprocesses. A callback that references a
`provider_call_id` not yet visible is **deferred** (no claim committed), never dropped.

**Outbound idempotency.** `telephony_calls.idempotency_key` unique per `organization_id`.
Two `create_call` requests under one key are the SAME logical call only when a canonical
**request fingerprint** matches — a SHA-256 over the semantic fields:

    provider_account_id · from_number_id · canonical destination · normalized metadata

`organization_id` is implicit (tenant scope + the per-organization unique key).
`correlation_id` is **observational** — a trace-propagation hint that never changes the
call placed — and is excluded. The fingerprint is persisted on
`telephony_calls.request_fingerprint` (`nexus_ai.telephony.idempotency`,
`OUTBOUND_FINGERPRINT_VERSION = 1`) and **all four** idempotency paths compare exactly
that one value — the top-level `_replay`, the top-of-transaction `by_idempotency_key`
check, the `IntegrityError` winner resolution, and the bounded-retry winner lookup —
never a partial subset.

* same key + **identical** fingerprint → the winner's call is replayed; a concurrent
  identical create is exactly one logical call and one provider send.
* same key + **any** semantic difference (a different caller-ID number, destination,
  provider account, or metadata) → deterministic `NXS_TELEPHONY_IDEMPOTENCY_CONFLICT`;
  a changed caller ID is never silently replayed onto the original call. Proven
  sequentially and under a concurrent same-key / different-`from_number_id` race
  (deterministic single winner + conflicting loser + exactly one provider send).

**Failure semantics.**

| Case | Behaviour |
|---|---|
| provider HTTP 4xx/5xx on create | call → `FAILED` (disposition `FAILED`, `error_code` set), `NXS_TELEPHONY_PROVIDER_ERROR` |
| **provider timeout AFTER the create request** | **ambiguous** — the call stays live with `error_code = NXS_TELEPHONY_PROVIDER_TIMEOUT`, `AmbiguousProviderTimeoutError` (504) is raised, and a retry under the same key **replays the record — a second call is NEVER placed** |
| provider outage during hangup | the local terminal transition (`ENDING`) still commits; the provider hangup is best-effort and logged |
| DB / outbox failure mid-transition | the whole tenant transaction rolls back — no partial state, no orphan event |
| provider sends an unknown `provider_call_id` | `deferred` (retryable), no claim |
| duplicate / stale / unsigned / wrong-secret webhook | `replayed` / `NXS_TELEPHONY_WEBHOOK_REPLAY` / `NXS_TELEPHONY_WEBHOOK_INVALID` — never ACKed as accepted |
| restart between provider response and persistence | the create transaction had already committed the `CREATED` call before the provider call; on restart the linked `provider_call_id` may be missing — the next provider callback re-links via `(account, provider_call_id)` or the call expires unlinked |

**Timeouts.** Every provider control operation is bounded by
`asyncio.timeout(settings.telephony.provider_timeout_seconds)`, the stricter of that and
the P07 executor timeout winning. No timeout field is decorative. Retries are never
applied to a non-idempotent outbound create.

**Webhook security.** `verify_signed_webhook`: `X-Telephony-Signature` (HMAC-SHA256 over
`"<unix_ts>." + body`) and `X-Telephony-Timestamp` are both MANDATORY; a missing /
malformed signature or timestamp → `NXS_TELEPHONY_WEBHOOK_INVALID`; a tampered body →
constant-time HMAC failure; a correctly-signed request outside
`± webhook_timestamp_tolerance_seconds` (default 300) → `NXS_TELEPHONY_WEBHOOK_REPLAY`.
Body size is bounded. No unsigned "accept all" mode exists.

**Media-session boundary for NXS-P12.** A `MediaSession` records `direction`, `state`
(`PENDING` → `ACTIVE` → `STOPPED`), `bridge_id`, `stream_id` and codec metadata — **no
audio, no recording**. On a provider `MEDIA` event the inbound service creates / stops a
session and emits `telephony.media.started` / `telephony.media.stopped`. **P12 attaches
an external voice stream** to an `ACTIVE` session's `bridge_id` (Asterisk
`externalMedia`), writes the media codec / stream identifiers it negotiates, and
consumes the `telephony.call.*` / `telephony.media.*` events — without changing any P11
contract. Call recording, if a future compliance policy authorizes it, is a separate
opt-in and is out of P11 and P12 scope.

**Events (P04 outbox, NXS-EVENT-011).** `telephony.call.{created,ringing,answered,
bridged,ending,completed,failed,busy,no_answer,cancelled}`, `telephony.dtmf.received`,
`telephony.media.{started,stopped}` — safe metadata only (call id, account id,
direction, provider, state, disposition, correlation id). No SIP / ARI credential, no
Authorization header, no provider token, no raw SDP, no sensitive payload.

Consequences: telephony state is durable, tenant-isolated and replay-safe; an ambiguous
carrier timeout never produces a duplicate call; and P12 has a stable media seam.
