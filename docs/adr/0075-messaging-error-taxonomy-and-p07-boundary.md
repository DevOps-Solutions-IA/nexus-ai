# ADR-0075: Messaging error taxonomy and the NXS-P07 boundary

Status: Accepted. Context: NXS-P09 needs a stable RFC 9457 error taxonomy, must never leak a provider secret or raw payload through an error, and must reuse — not reinvent — the NXS-P07 credential and governed-transport boundary.

Decision:

**Stable `NXS_MSG_*` taxonomy** (`nexus_ai.messaging.errors.MESSAGING_ERRORS`, frozen): account (`ACCOUNT_NOT_FOUND` 404, `ACCOUNT_DISABLED` 409, `ACCOUNT_CONFLICT` 409, `CONFIG_INVALID` 422), message (`MESSAGE_NOT_FOUND` 404, `CONVERSATION_INVALID` 409, `RECIPIENT_INVALID` 422, `PAYLOAD_INVALID` 422), webhook auth (`AUTH_FAILED` 401, `SIGNATURE_INVALID` 401, `REPLAY_REJECTED` 409, `CHALLENGE_FAILED` 403), idempotency (`IDEMPOTENCY_CONFLICT` 409, `SEND_IN_PROGRESS` 409 retryable), delivery (`PROVIDER_ERROR` 502, `DELIVERY_FAILED` 502, `TIMEOUT` 504 *not* retryable — ambiguous, `RATE_LIMITED` 429 retryable), `STATE_CONFLICT` 409. Every code is `NXS_MSG_`-prefixed, has an HTTP status, a safe title and a `retryable` flag; rendered to RFC 9457 Problem Details by the app-wide `NxsError` handler.

**A raw provider error is never re-exported.** `MessagingProviderError` carries only `extensions.provider_code` and `provider_status` (bounded) — never the provider response body, headers or any secret. `provider_send_error` collapses HTTP status into the taxonomy at the adapter boundary.

**Reuse of NXS-P07, not reinvention.**
- *Credentials*: messaging accounts store a `credential_ref` and resolve `SecretMaterial` through `LocalEncryptedVault` — the exact NXS-P07 vault class, Fernet encryption (`build_fernet`) and `EncryptedSecretStore` protocol. A new `CredentialType.PROVIDER_SECRET_SET` (no fixed field schema) and a dedicated `messaging_secrets` table isolate the messaging credential plane from the integration plane, exactly as NXS-P07 itself uses a dedicated `integration_secrets` table. `SecretMaterial` still redacts in `repr`/`str` and is never a serialisable model.
- *Transport*: every outbound provider HTTP call goes through `GovernedMessagingTransport`, a thin adapter over the NXS-P07 `GovernedHttpExecutor` (SSRF allow-list, bounded timeouts, TLS verification, redirect revalidation, reserved-header stripping). P09 opens no socket of its own and exposes no arbitrary-URL / method / header surface.
- *Webhook seam*: the tenant-scoped `build_webhook_token` / `decode_webhook_token` and constant-time HMAC helpers are reused from NXS-P07's inbound webhook foundation.

**Not the NXS-P07 execution registry.** P09 does not route sends through `IntegrationHubService.execute` — a messaging account is not an integration operation. The P07 boundary that matters (credentials, governed transport, webhook seam) is reused; the operation-registry model is not imposed on a channel.

Consequences: a caller (and a future Agent Runtime / Workflow Engine) branches on a small closed code set; a provider outage never leaks upstream detail; there is one secret store implementation and one governed HTTP path in the product.
