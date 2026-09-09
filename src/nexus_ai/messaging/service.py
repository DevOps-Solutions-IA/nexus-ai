"""Messaging channel account management + the governed outbound pipeline (NXS-P09).

Outbound: caller -> organization / channel policy -> conversation + customer validation
-> durable idempotency (replay same key+fingerprint; deterministic conflict on a
different fingerprint; NOT exactly-once; an ambiguous provider timeout is honest and a
non-idempotent send is never blindly retried) -> provider adapter (governed transport,
no bypass) -> normalized result -> message state + receipt -> P04 event.

The Customer / Conversation / timeline model is NXS-P06's; this service resolves and
attaches to it, never re-implements it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import ConversationNotFoundError, NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.customers.entities import (
    ActivityType,
    ConversationStatus,
    CreateConversationRequest,
    CreateCustomerRequest,
)
from nexus_ai.domain.customers.repository import ConversationActivityRepository
from nexus_ai.domain.customers.service import ConversationService, CustomerService
from nexus_ai.domain.messaging.repository import (
    MessagingAccountRepository,
    MessagingMessageRepository,
    MessagingSendIdempotencyRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial, VaultClient
from nexus_ai.integrations.webhooks import build_webhook_token
from nexus_ai.messaging.account_config import validate_account_configuration
from nexus_ai.messaging.addresses import identity_type_for, normalize_address
from nexus_ai.messaging.content import normalize_content, sanitize_header_value
from nexus_ai.messaging.entities import (
    AccountStatus,
    CreateAccountRequest,
    EmailEnvelopeFields,
    Message,
    MessageChannel,
    MessageContentType,
    MessageDirection,
    MessageStatus,
    MessagingAccount,
    SendMessageRequest,
    StoreAccountCredentialRequest,
    UpdateAccountRequest,
)
from nexus_ai.messaging.errors import (
    MessageNotFoundError,
    MessagingAccountConflictError,
    MessagingAccountDisabledError,
    MessagingAccountNotFoundError,
    MessagingConfigInvalidError,
    MessagingConversationInvalidError,
    MessagingDeliveryFailedError,
    MessagingIdempotencyConflictError,
    MessagingRecipientInvalidError,
    MessagingSendInProgressError,
)
from nexus_ai.messaging.idempotency import (
    SendIdempotencyRecord,
    SendIdempotencyStatus,
    send_fingerprint,
)
from nexus_ai.messaging.providers.base import MessagingTransport, PreparedSend, ProviderSendResult
from nexus_ai.messaging.providers.registry import DEFAULT_PROVIDER, resolve_provider

_SINGLE_RECIPIENT_CHANNELS = {MessageChannel.WHATSAPP, MessageChannel.SMS}


class MessagingService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
        transport: MessagingTransport,
        customers: CustomerService,
        conversations: ConversationService,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._transport = transport
        self._customers = customers
        self._conversations = conversations
        self._log = get_logger("nexus_ai.messaging")

    # -- accounts ----------------------------------------------------------------

    async def create_account(
        self, organization_id: UUID, request: CreateAccountRequest
    ) -> MessagingAccount:
        provider_key = request.provider or DEFAULT_PROVIDER[request.channel]
        resolve_provider(request.channel, provider_key)  # fail closed on an unknown provider
        try:
            sender = normalize_address(
                request.channel, request.sender_identity, default_country=request.default_country
            )
        except MessagingRecipientInvalidError as exc:
            raise MessagingConfigInvalidError(
                "the sender identity is not valid for this channel"
            ) from exc
        configuration = dict(request.configuration)
        if request.default_country and "default_country" not in configuration:
            configuration["default_country"] = request.default_country
        configuration = validate_account_configuration(
            request.channel, provider_key, configuration, settings=self._settings.channels
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await MessagingAccountRepository(tenant).insert(
                account_id=uuid.uuid7(),
                channel=request.channel,
                provider=provider_key,
                slug=request.slug,
                external_account_id=request.external_account_id,
                sender_identity=sender.value,
                configuration=configuration,
                webhook_token=build_webhook_token(organization_id),
            )

    async def list_accounts(self, organization_id: UUID, *, limit: int) -> list[MessagingAccount]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await MessagingAccountRepository(tenant).list_all(limit=limit)

    async def get_account(self, organization_id: UUID, account_id: UUID) -> MessagingAccount:
        return await self._require_account(organization_id, account_id)

    async def update_account(
        self, organization_id: UUID, account_id: UUID, request: UpdateAccountRequest
    ) -> MessagingAccount:
        account = await self._require_account(organization_id, account_id)
        if request.configuration is None:
            return account
        configuration = validate_account_configuration(
            account.channel,
            account.provider,
            dict(request.configuration),
            settings=self._settings.channels,
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await MessagingAccountRepository(tenant).apply(
                account_id, changes={"configuration": configuration}
            )
        assert updated is not None  # noqa: S101 - _require_account proved existence
        return updated

    async def set_account_status(
        self, organization_id: UUID, account_id: UUID, status: AccountStatus
    ) -> MessagingAccount:
        await self._require_account(organization_id, account_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await MessagingAccountRepository(tenant).apply(
                account_id, changes={"status": status.value}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def delete_account(self, organization_id: UUID, account_id: UUID) -> None:
        account = await self._require_account(organization_id, account_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            has_history = (
                await tenant.session.execute(
                    text("SELECT 1 FROM messaging_messages WHERE account_id = :a LIMIT 1"),
                    {"a": account_id},
                )
            ).first() is not None
        if has_history:
            raise MessagingAccountConflictError(
                "the messaging account has message history and cannot be deleted; disable it"
            )
        if account.credential_ref is not None:
            await self._vault.delete_secret(organization_id, account.credential_ref)
        async with self._db.tenant_transaction(organization_id) as tenant:
            removed = await MessagingAccountRepository(tenant).delete_one(account_id)
        if not removed:
            raise MessagingAccountNotFoundError("the messaging account does not exist")

    async def store_account_credential(
        self, organization_id: UUID, account_id: UUID, request: StoreAccountCredentialRequest
    ) -> None:
        account = await self._require_account(organization_id, account_id)
        ref = account.credential_ref or f"msg:{account.slug}:{account_id.hex[:12]}"
        await self._vault.store_secret(
            organization_id,
            ref,
            SecretMaterial(CredentialType.PROVIDER_SECRET_SET, dict(request.fields)),
        )
        if account.credential_ref != ref:
            async with self._db.tenant_transaction(organization_id) as tenant:
                await MessagingAccountRepository(tenant).apply(
                    account_id, changes={"credential_ref": ref}
                )

    async def delete_account_credential(self, organization_id: UUID, account_id: UUID) -> None:
        account = await self._require_account(organization_id, account_id)
        if account.credential_ref is None:
            return
        await self._vault.delete_secret(organization_id, account.credential_ref)
        async with self._db.tenant_transaction(organization_id) as tenant:
            await MessagingAccountRepository(tenant).apply(
                account_id, changes={"credential_ref": None}
            )

    # -- message queries -------------------------------------------------------

    async def get_message(self, organization_id: UUID, message_id: UUID) -> Message:
        async with self._db.tenant_transaction(organization_id) as tenant:
            message = await MessagingMessageRepository(tenant).by_id(message_id)
        if message is None:
            raise MessageNotFoundError("the message does not exist in this Organization")
        return message

    async def list_messages(
        self,
        organization_id: UUID,
        conversation_id: UUID,
        *,
        after_id: UUID | None,
        limit: int,
    ) -> list[Message]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            rows = await MessagingMessageRepository(tenant).list_for_conversation(
                conversation_id, after_id=after_id, limit=limit
            )
        return rows[:limit]

    # -- outbound send --------------------------------------------------------

    async def send(
        self, organization_id: UUID, actor_user_id: UUID | None, request: SendMessageRequest
    ) -> Message:
        account = await self._require_account(organization_id, request.account_id)
        if account.status is not AccountStatus.ACTIVE:
            raise MessagingAccountDisabledError("the messaging account is disabled")

        conversation = await self._require_conversation(
            organization_id, request.conversation_id, account.channel
        )
        parent = await self._require_reply_parent(
            organization_id, conversation, account.channel, request.reply_to_message_id
        )
        recipients = self._normalized_recipients(account, request)
        content = normalize_content(account.channel, request.content)
        subject = self._normalized_subject(account.channel, request)
        email_fields = self._email_thread_fields(account.channel, subject, parent)

        fingerprint = send_fingerprint(request, [address.value for address in recipients])
        if request.idempotency_key is not None:
            replay = await self._claim(organization_id, account.id, request, fingerprint)
            if replay is not None:
                return replay

        message = await self._persist_queued(
            organization_id, account, conversation, recipients, content, email_fields, request
        )

        secret = await self._resolve_secret(organization_id, account)
        prepared = self._prepare(account, recipients, content, subject, email_fields)
        provider = resolve_provider(account.channel, account.provider)

        try:
            result = await provider.send(account, prepared, secret, self._transport)
        except NxsError as exc:
            failed = await self._mark_failed(organization_id, account, message, exc)
            if request.idempotency_key is not None:
                await self._finalize(
                    organization_id,
                    account.id,
                    request.idempotency_key,
                    SendIdempotencyStatus.FAILED,
                    failed.id,
                    None,
                    exc.code if isinstance(exc, NxsError) else "NXS_MSG_DELIVERY_FAILED",
                )
            raise
        sent = await self._mark_sent(organization_id, account, message, result)
        if request.idempotency_key is not None:
            await self._finalize(
                organization_id,
                account.id,
                request.idempotency_key,
                SendIdempotencyStatus.COMPLETED,
                sent.id,
                sent.public_view().model_dump(mode="json"),
                None,
            )
        return sent

    # -- helpers -------------------------------------------------------------

    async def _require_account(self, organization_id: UUID, account_id: UUID) -> MessagingAccount:
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await MessagingAccountRepository(tenant).by_id(account_id)
        if account is None:
            raise MessagingAccountNotFoundError(
                "the messaging account does not exist in this Organization"
            )
        return account

    async def _require_conversation(
        self, organization_id: UUID, conversation_id: UUID, channel: MessageChannel
    ) -> Any:
        try:
            conversation = await self._conversations.get(organization_id, conversation_id)
        except ConversationNotFoundError as exc:
            raise MessagingConversationInvalidError(
                "the conversation does not exist in this Organization"
            ) from exc
        if conversation.channel != channel.value.lower():
            raise MessagingConversationInvalidError(
                "the conversation belongs to a different channel"
            )
        if conversation.status is ConversationStatus.CLOSED:
            raise MessagingConversationInvalidError("the conversation is closed")
        return conversation

    def _normalized_recipients(
        self, account: MessagingAccount, request: SendMessageRequest
    ) -> list[Any]:
        default_country = account.configuration.get("default_country")
        recipients = [
            normalize_address(
                account.channel, item.value, default_country=default_country, display=item.display
            )
            for item in request.to
        ]
        if account.channel in _SINGLE_RECIPIENT_CHANNELS and len(recipients) != 1:
            raise MessagingRecipientInvalidError(
                f"{account.channel.value} delivers to exactly one recipient per message"
            )
        return recipients

    @staticmethod
    def _normalized_subject(channel: MessageChannel, request: SendMessageRequest) -> str | None:
        if request.subject is None:
            return None
        if channel is not MessageChannel.EMAIL:
            raise MessagingConfigInvalidError(f"{channel.value} messages do not carry a subject")
        return sanitize_header_value(request.subject, field="subject")

    async def _resolve_secret(
        self, organization_id: UUID, account: MessagingAccount
    ) -> SecretMaterial | None:
        if account.credential_ref is None:
            return None
        return await self._vault.get_secret(organization_id, account.credential_ref)

    async def _claim(
        self,
        organization_id: UUID,
        account_id: UUID,
        request: SendMessageRequest,
        fingerprint: str,
    ) -> Message | None:
        assert request.idempotency_key is not None  # noqa: S101
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=self._settings.channels.send_idempotency_retention_seconds
        )
        outcome = await MessagingSendIdempotencyRepository(self._db).claim(
            organization_id=organization_id,
            account_id=account_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            expires_at=expires_at,
        )
        if outcome.is_owner:
            return None
        existing = outcome.existing
        if existing is None:  # pragma: no cover - a conflict implies a row
            raise MessagingSendInProgressError("the idempotency claim is being finalised")
        return self._replay_or_raise(organization_id, existing, fingerprint)

    def _replay_or_raise(
        self, organization_id: UUID, existing: SendIdempotencyRecord, fingerprint: str
    ) -> Message:
        if existing.request_fingerprint != fingerprint:
            raise MessagingIdempotencyConflictError(
                "this idempotency key was already used with a different message"
            )
        if existing.status is SendIdempotencyStatus.COMPLETED and existing.result_json is not None:
            return Message.model_validate(existing.result_json)
        if existing.status is SendIdempotencyStatus.FAILED:
            raise MessagingDeliveryFailedError(
                "a previous send with this idempotency key failed; use a new key",
                extensions={
                    "original_error_code": existing.error_code or "NXS_MSG_DELIVERY_FAILED"
                },
            )
        raise MessagingSendInProgressError(
            "a send with this idempotency key is still in progress; retry shortly"
        )

    async def _require_reply_parent(
        self,
        organization_id: UUID,
        conversation: Any,
        channel: MessageChannel,
        reply_to_message_id: UUID | None,
    ) -> Message | None:
        """Resolve and govern a reply parent. A parent must exist in this Organization
        (RLS + the tenant-aware self-reference FK enforce it), belong to the SAME
        Conversation, and — for an Email reply — itself be an Email message. A new root
        message (no reply_to) is fine."""
        if reply_to_message_id is None:
            return None
        async with self._db.tenant_transaction(organization_id) as tenant:
            parent = await MessagingMessageRepository(tenant).by_id(reply_to_message_id)
        if parent is None:
            raise MessagingConversationInvalidError(
                "the reply_to_message_id does not exist in this Organization"
            )
        if parent.conversation_id != conversation.id:
            raise MessagingConversationInvalidError(
                "the reply_to message belongs to a different Conversation"
            )
        if channel is MessageChannel.EMAIL and parent.channel is not MessageChannel.EMAIL:
            raise MessagingConversationInvalidError(
                "an Email reply may only reference an Email message"
            )
        return parent

    def _email_thread_fields(
        self, channel: MessageChannel, subject: str | None, parent: Message | None
    ) -> EmailEnvelopeFields | None:
        """Build the outbound Email envelope. ``message_id_header`` is the CANONICAL
        threading identifier for a Nexus message (set on every outbound Email and copied
        from the provider id on every inbound Email); ``provider_message_id`` is the
        provider's own opaque id and is NOT used for threading. In-Reply-To / References
        are derived from the parent and are CRLF / control-character safe."""
        if channel is not MessageChannel.EMAIL:
            return None
        our_message_id = f"{uuid.uuid7().hex}@nexus.messaging"
        if parent is None or parent.email is None:
            return EmailEnvelopeFields(subject=subject, message_id_header=our_message_id)
        parent_id = parent.email.message_id_header or parent.provider_message_id or ""
        parent_id = sanitize_header_value(parent_id, field="parent message id", max_length=255)
        references = [
            sanitize_header_value(ref, field="references entry", max_length=255)
            for ref in parent.email.references
            if ref
        ]
        if parent_id and parent_id not in references:
            references.append(parent_id)
        return EmailEnvelopeFields(
            subject=subject,
            message_id_header=our_message_id,
            in_reply_to=parent_id or None,
            references=tuple(references[-20:]),
        )

    async def _persist_queued(
        self,
        organization_id: UUID,
        account: MessagingAccount,
        conversation: Any,
        recipients: list[Any],
        content: Any,
        email: EmailEnvelopeFields | None,
        request: SendMessageRequest,
    ) -> Message:
        now = dt.datetime.now(dt.UTC)
        ctx = current_context()
        message = Message(
            id=uuid.uuid7(),
            organization_id=organization_id,
            conversation_id=conversation.id,
            customer_id=conversation.customer_id,
            channel=account.channel,
            direction=MessageDirection.OUTBOUND,
            status=MessageStatus.QUEUED,
            provider=account.provider,
            provider_account_id=account.id,
            provider_message_id=None,
            sender=normalize_address(account.channel, account.sender_identity),
            recipients=tuple(recipients),
            content=content,
            email=email,
            sms=None,
            reply_to_message_id=request.reply_to_message_id,
            correlation_id=request.correlation_id or (None if ctx is None else ctx.correlation_id),
            idempotency_key=request.idempotency_key,
            error_code=None,
            provider_timestamp=None,
            sent_at=None,
            delivered_at=None,
            read_at=None,
            failed_at=None,
            created_at=now,
            updated_at=now,
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            stored = await MessagingMessageRepository(tenant).insert(message)
            await ConversationActivityRepository(tenant).append(
                customer_id=stored.customer_id,
                conversation_id=stored.conversation_id,
                activity_type=ActivityType.MESSAGE_OUTBOUND,
                dedup_key=f"messaging.outbound:{stored.id}",
                data={"channel": stored.channel.value, "message_id": str(stored.id)},
            )
            await self._enqueue_message_event(
                tenant.session, organization_id, "messaging.message.queued", stored
            )
        return stored

    def _prepare(
        self,
        account: MessagingAccount,
        recipients: list[Any],
        content: Any,
        subject: str | None,
        email: EmailEnvelopeFields | None,
    ) -> PreparedSend:
        return PreparedSend(
            sender=normalize_address(account.channel, account.sender_identity),
            recipients=tuple(recipients),
            content=content,
            subject=subject,
            email=email,
            reply_provider_message_id=(None if email is None else email.in_reply_to),
        )

    async def _mark_sent(
        self,
        organization_id: UUID,
        account: MessagingAccount,
        message: Message,
        result: ProviderSendResult,
    ) -> Message:
        now = dt.datetime.now(dt.UTC)
        changes: dict[str, Any] = {
            "status": MessageStatus.SENT.value,
            "provider_message_id": result.provider_message_id[:200],
            "sent_at": now,
            "provider_timestamp": result.provider_timestamp,
        }
        if result.sms is not None:
            changes["sms"] = result.sms.model_dump(mode="json")
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await MessagingMessageRepository(tenant).apply(message.id, changes)
            assert updated is not None  # noqa: S101
            await self._enqueue_message_event(
                tenant.session, organization_id, "messaging.message.sent", updated
            )
        return updated

    async def _mark_failed(
        self,
        organization_id: UUID,
        account: MessagingAccount,
        message: Message,
        error: NxsError,
    ) -> Message:
        now = dt.datetime.now(dt.UTC)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await MessagingMessageRepository(tenant).apply(
                message.id,
                {
                    "status": MessageStatus.FAILED.value,
                    "failed_at": now,
                    "error_code": error.code,
                },
            )
            assert updated is not None  # noqa: S101
            await self._enqueue_message_event(
                tenant.session,
                organization_id,
                "messaging.message.failed",
                updated,
                extra={
                    "error_code": error.code,
                    "provider_code": error.extensions.get("provider_code"),
                },
            )
        return updated

    async def _finalize(
        self,
        organization_id: UUID,
        account_id: UUID,
        idempotency_key: str,
        status: SendIdempotencyStatus,
        message_id: UUID | None,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        await MessagingSendIdempotencyRepository(self._db).finalize(
            organization_id=organization_id,
            account_id=account_id,
            idempotency_key=idempotency_key,
            status=status,
            message_id=message_id,
            result_json=result_json,
            error_code=error_code,
        )

    async def _enqueue_message_event(
        self,
        session: Any,
        organization_id: UUID,
        event_type: str,
        message: Message,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        ctx = current_context()
        payload: dict[str, Any] = {
            "message_id": str(message.id),
            "conversation_id": str(message.conversation_id),
            "channel": message.channel.value,
            "direction": message.direction.value,
            "provider": message.provider,
            "provider_message_id": message.provider_message_id,
        }
        if message.direction is MessageDirection.INBOUND:
            payload["customer_id"] = (
                None if message.customer_id is None else str(message.customer_id)
            )
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})
            if "error_code" in extra and extra["error_code"] is not None:
                payload["error_code"] = extra["error_code"]
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="messaging_message",
            aggregate_id=str(message.id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)


# --- shared inbound helpers (used by the webhook service) --------------------


async def resolve_inbound_customer(
    customers: CustomerService,
    organization_id: UUID,
    channel: MessageChannel,
    address_value: str,
    display: str | None,
) -> uuid.UUID:
    identity_type = identity_type_for(channel)
    customer, _ = await customers.resolve_or_create(
        organization_id,
        CreateCustomerRequest(
            display_name=(display or address_value)[:200],
            identity_type=identity_type,
            identity_value=address_value,
            identity_source=f"messaging_{channel.value.lower()}",
        ),
    )
    return customer.id


async def resolve_inbound_conversation(
    conversations: ConversationService,
    organization_id: UUID,
    account: MessagingAccount,
    customer_id: uuid.UUID,
    external_thread_hint: str,
) -> Any:
    conversation, _ = await conversations.open_or_resolve(
        organization_id,
        CreateConversationRequest(
            customer_id=customer_id,
            channel=account.channel.value.lower(),
            provider_namespace=account.provider[:47],
            external_thread_id=external_thread_hint[:128],
        ),
    )
    return conversation


def content_type_label(content_type: MessageContentType) -> str:
    return content_type.value
