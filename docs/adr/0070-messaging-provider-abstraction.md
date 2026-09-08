# ADR-0070: Messaging provider abstraction

Status: Accepted. Context: NXS-P09 must be provider-neutral — a provider-specific payload must never become the canonical domain contract — while WhatsApp, Email and SMS differ enough that forcing an identical transport API would be wrong.

Decision:

**A provider is a normalizer, not a transport.** `MessagingProvider` (in `nexus_ai.messaging.providers.base`) declares `webhook_challenge`, `verify_webhook`, `parse_webhook` and `send`. It forces an identical *normalized domain output* (the shared `MessageAddress`, `MessageContent`, `MediaMetadata`, `EmailEnvelopeFields`, `SmsSegmentInfo`, `NormalizedInbound`, `NormalizedStatus`, `ProviderSendResult`), not an identical wire API.

**No provider opens its own socket.** Every outbound HTTP call goes through the injected `MessagingTransport`. Production wires `GovernedMessagingTransport`, which adapts the NXS-P07 `GovernedHttpExecutor` — so a provider call inherits the Hub's SSRF allow-list, bounded connect / read / total timeouts, TLS verification, redirect revalidation and reserved-header stripping. A transport failure surfaces as `TransportError` with an honest `timeout` / `connect` ambiguity flag.

**Reference adapters.** `WhatsAppProvider` targets the Meta WhatsApp Cloud API (`/{phone_number_id}/messages`, `X-Hub-Signature-256` app-secret HMAC, `hub.challenge` GET verification, `entry[].changes[].value.messages/statuses`). `EmailProvider` and `SmsProvider` target the common JSON-over-HTTPS shape of transactional providers (Postmark / SendGrid / Twilio-style), with an `X-Messaging-Signature` HMAC webhook. A provider is resolved by `(channel, provider_key)` from a static registry; an unknown provider fails closed with `NXS_MSG_CONFIG_INVALID`.

**Untrusted provider input.** `parse_webhook` never trusts an `organization_id` in the body. WhatsApp media is normalized to bounded `MediaMetadata` where the provider-supplied MIME type, filename and size are *declared hints* only — never a security decision, and never a URL fetched through unsafe code. Provider HTTP status is mapped to the shared taxonomy by `provider_send_error` (429 → `NXS_MSG_RATE_LIMITED`, 504 → `NXS_MSG_TIMEOUT`, else `NXS_MSG_PROVIDER_ERROR` retryable only on 5xx).

**Secrets.** A provider receives a `SecretMaterial` (resolved from the vault) for exactly the call that needs it and never persists it. The bearer token reaches the provider's HTTP request; it never reaches a `messaging_messages` row, an event or a log.

Consequences: adding WhatsApp on-premise, a different email provider or a different SMS provider is a new adapter class registered under its channel — the domain model, the pipelines, the persistence and the API are untouched.
