# ADR-0069: Messaging Channels architecture

Status: Accepted. Context: NXS-P09 (NXS-WA-001, NXS-EMAIL-001, NXS-SMS-001) needs a permanent enterprise messaging subsystem for WhatsApp, Email and SMS — inbound and outbound — that preserves Organization isolation, unified customer identity, the shared NXS-P06 conversation model, the governed NXS-P07 provider / credential boundary, provider neutrality, deterministic idempotency, webhook authenticity, replay protection, delivery-state tracking, normalized events, an auditable error taxonomy and safe secret handling.

Decision:

**Two governed pipelines, one message model.**

Inbound: `provider webhook → channel adapter → authenticity / signature verification → tenant + provider-account resolution → payload-size bound → provider-payload normalization → dedupe / replay / idempotency → NXS-P06 customer identity resolution → NXS-P06 conversation / thread resolution → normalized message persistence → P04 transactional outbox event → provider ACK`. A security-invalid webhook is never ACKed as accepted; no Customer or Conversation is created before authenticity passes; the provider payload never names the trusted `organization_id` (it comes from the unguessable token).

Outbound: `internal caller → MessagingService → organization / channel policy → conversation + customer validation → durable idempotency → provider adapter → provider → normalized provider result → message state + receipt → P04 event`.

**P06 vs P09 ownership.** NXS-P06 remains authoritative for Customer, Identity, Conversation, participants and the timeline. P09 owns channel accounts, messaging provider abstractions, inbound normalization, outbound delivery, `messaging_messages` records, provider message identifiers, the delivery lifecycle, webhook verification, channel-specific metadata, deduplication / idempotency and channel events. P09 never creates a second customer or conversation model — it resolves and attaches to P06's, and its resolution calls are P06's duplicate-safe `resolve_or_create` / `open_or_resolve`.

**Persistence.** Five tenant-owned tables with forced RLS: `messaging_accounts`, `messaging_messages`, `messaging_inbound_receipts`, `messaging_send_idempotency`, `messaging_secrets`. `messaging_messages` uses composite tenant-aware FKs `(organization_id, <parent_id>) → (organization_id, id)` to `conversations`, `customers` and `messaging_accounts`. A provider account (`channel + provider + external_account_id`) and a webhook routing token are GLOBALLY unique — no two Organizations can adopt the same phone number, mailbox or token.

**API.** Governed only: channel-account CRUD + credentials, `POST /api/v1/messaging/messages`, message reads, and `/api/v1/webhooks/messaging/{provider}/{token}`. There is no raw provider-request endpoint, no arbitrary URL / method / header surface, no way to read a provider access token, no raw SMTP command surface and no callback forwarding. New RBAC: `messaging:read`, `messaging:send`, `messaging:manage_accounts` (org_member gets read + send).

**Events.** `messaging.message.received / queued / sent / delivered / read / failed` — ID + safe metadata only, no body, no address, no subject, no provider token, through the P04 transactional outbox in the same transaction as the mutation.

Consequences: a new provider is a channel adapter plus a `MessagingAccount` configuration; the future NXS-P10 OTP layer consumes the generic SMS delivery mechanism without P09 knowing anything about OTP; NXS-P13 / P14 / P16 build on the message primitives without P09 coupling to any of them.
