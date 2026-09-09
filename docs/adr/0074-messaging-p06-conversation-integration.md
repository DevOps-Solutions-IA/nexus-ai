# ADR-0074: Messaging ↔ NXS-P06 conversation integration

Status: Accepted. Context: NXS-P09 must attach channel messages to the ONE customer identity plane and the ONE conversation model from NXS-P06 — never a second copy — and must do so race-safely under concurrent inbound webhooks.

Decision:

**One normalization vocabulary.** A channel address is normalized through the NXS-P06 `normalize_identity_value` (E.164 for phone, trim + lowercase for email) — the same function the Customer plane already indexes. A WhatsApp / SMS sender and an email `From` resolve to exactly the canonical identity a manual customer creation would.

**Inbound resolution.**
- `resolve_inbound_customer` → NXS-P06 `CustomerService.resolve_or_create` with the normalized sender as the initial identity and `source = "messaging_<channel>"`. Concurrent first-contact webhooks converge on one Customer (P06 race-recovery corrective).
- `resolve_inbound_conversation` → NXS-P06 `ConversationService.open_or_resolve` with `channel`, `provider_namespace = provider_key`, `external_thread_id = <adapter thread hint>`. The thread hint is deterministic per channel: an email `References` root (so a reply chain threads to one Conversation), a sorted WhatsApp / SMS phone pair. The P06 `UNIQUE(organization_id, channel, provider_namespace, external_thread_id)` makes "one Conversation per external thread" a database guarantee, and `_assert_thread_compatible` fails closed on a customer mismatch.

**Attachment.** A `messaging_messages` row carries `conversation_id` (composite tenant-aware FK to `conversations`) and `customer_id` (composite tenant-aware FK to `customers`, nullable). A `message.inbound` / `message.outbound` activity is appended to the P06 timeline with a `dedup_key` so a replayed webhook appends exactly once. So a channel message is visible through the shared conversation context (the P06 timeline) without P09 owning any of it.

**Not built.** P09 does not add a customer model, a conversation model, a participant model or a timeline model. It does not add customer merge, identity verification or conversation assignment — those stay P06 / P10 / P17.

**Proven** (`tests/integration/test_messaging_channels.py`, `tests/concurrency/test_messaging_concurrency.py`): a known identity maps inbound to the right Customer; email replies thread deterministically; concurrent inbound creates exactly one Customer; a channel message shows on the shared timeline.

Consequences: the customer graph stays single-sourced; adding a channel does not fork identity or conversation logic.
