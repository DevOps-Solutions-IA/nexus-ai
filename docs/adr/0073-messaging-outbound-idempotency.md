# ADR-0073: Messaging outbound idempotency

Status: Accepted. Context: NXS-P09 outbound sends have real external side effects. The directive is explicit: support a durable idempotency key; same key + same semantic request replays the prior result; same key + different request is a deterministic conflict; do NOT claim exactly-once network delivery; a provider timeout is ambiguous and a non-idempotent send is never blindly re-attempted.

Decision:

**Durable claim-based store.** `messaging_send_idempotency` is a tenant-owned table with forced RLS and `UNIQUE(organization_id, account_id, idempotency_key)`. `MessagingSendIdempotencyRepository.claim` uses the NXS-P06 race-recovery pattern: a lost unique-constraint race propagates out of the transaction (full rollback), then a fresh transaction re-resolves the committed winner.

**Fingerprint.** `send_fingerprint` is a SHA-256 over the canonical semantic request — account, conversation, *sorted* recipients, content (type + text + html + media), subject and reply-to. Recipient order does not change the fingerprint; content does.

**Outcomes:**
- `claim` succeeds (owner) → the send proceeds; on success the claim is finalised `COMPLETED` with the serialised `Message`; on a raised `NxsError` it is finalised `FAILED` with the error code.
- claim lost, existing `COMPLETED`, **same fingerprint** → the stored `Message` is returned (replay).
- claim lost, existing, **different fingerprint** → `NXS_MSG_IDEMPOTENCY_CONFLICT` (deterministic; never a silent second send).
- claim lost, existing `PENDING` → `NXS_MSG_SEND_IN_PROGRESS` (retryable).
- claim lost, existing `FAILED` → `NXS_MSG_DELIVERY_FAILED` carrying `original_error_code` — the caller must use a **new** key; the same key never silently re-sends.

**Ambiguity is honest.** A provider timeout raises `NXS_MSG_TIMEOUT`, marks the message `FAILED` and finalises the claim `FAILED` — the send may or may not have reached the provider, and a retry under the same key deterministically fails rather than risking a duplicate. This is de-duplication, not exactly-once.

**Proven** (`tests/concurrency/test_messaging_concurrency.py`, `tests/resilience/test_messaging_resilience.py`): 6 concurrent identical sends → 1 upstream call + 1 message + 1 `COMPLETED` record; different payload same key → conflict; timeout → `FAILED` claim, no blind resend on retry.

Consequences: a client that retries after a partial failure gets a deterministic answer, not a duplicate WhatsApp / SMS / email; the guarantee offered is "durable de-duplication".
