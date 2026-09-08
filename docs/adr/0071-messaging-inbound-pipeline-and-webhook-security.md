# ADR-0071: Messaging inbound pipeline and webhook security

Status: Accepted. Context: NXS-P09 inbound webhooks are the untrusted edge of the messaging subsystem. The pipeline must be deterministic, must fail closed, and must never let a hostile payload create tenant data or forge a tenant.

Decision:

**Deterministic order** (`InboundMessagingService.receive`):
1. identify the provider from the route (`/webhooks/messaging/{provider}/{token}`);
2. decode the tenant scope from the unguessable token — reusing the NXS-P07 `build_webhook_token` / `decode_webhook_token` seam, where a base64 Organization-id prefix binds the scope BEFORE any RLS lookup and the body is never consulted for tenant mapping;
3. resolve the provider account RLS-scoped by token — an unknown account or a route/provider mismatch is `NXS_MSG_AUTH_FAILED`;
4. size-bound the body to `settings.channels.max_webhook_body_bytes`;
5. run the provider's `verify_webhook` — signature (constant-time HMAC-SHA256), verify token (WhatsApp GET), timestamp tolerance where the protocol carries one. An unsigned request to a signed endpoint is rejected, never treated as verified;
6. reject a disabled account — AFTER verification, so a probe learns nothing;
7. `parse_webhook` into shared domain objects;
8. per event: a durable dedupe / replay claim keyed `(organization_id, account_id, event_id)`;
9. NXS-P06 customer identity resolution from the normalized inbound sender;
10. NXS-P06 conversation / thread resolution;
11. persist the normalized inbound message + timeline activity + `messaging.message.received` event **atomically with the receipt claim** — a crash mid-processing rolls all of it back and a provider retry reprocesses cleanly;
12. fold delivery-status callbacks into the canonical message state (ADR-0072).

**Guarantees.** No Customer or Conversation is created before step 5 passes. A concurrent duplicate webhook: one caller wins the receipt claim's unique constraint and processes; every other gets `replayed`, no second message row, no customer race (P06 `resolve_or_create` converges). A provider `organization_id` in the payload is inert. Provider ACK follows provider semantics (`202` / echoed `hub.challenge`); a security-invalid webhook raises and is never ACKed as accepted.

**Adversarial coverage** (`tests/security/test_messaging_security.py`): invalid / missing / wrong-secret signature, unknown token, wrong provider route, forged `organization_id`, cross-tenant account + message lookup, send through another org's account, provider-account adoption by a second org, oversized body, malformed JSON, disabled-account inbound, secret / token non-leakage into records and events, idempotency-key reuse with a different payload, stale-timestamp signature.

Consequences: the inbound edge has one shape regardless of channel; the receipt table is the single replay-protection primitive; the token is the single tenant-authority.
