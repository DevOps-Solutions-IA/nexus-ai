"""Inbound telephony ingestion (NXS-P11: NXS-TEL-001).

    provider callback -> webhook token -> account resolution -> signature + freshness
    verification -> provider normalization -> Organization / account resolution (fail
    closed) -> durable per-event idempotency claim -> call lookup / create -> monotonic
    state fold -> media-session fold -> P04 outbox event -> provider ACK

A security-invalid callback is NEVER ACKed as accepted. No call is created before
authenticity passes. The trusted ``organization_id`` comes from the webhook token, never
from the provider payload. The idempotency claim + state fold + outbox event commit
ATOMICALLY in one tenant transaction — a crash rolls the claim back and the provider
retry reprocesses.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.telephony.repository import (
    TelephonyAccountRepository,
    TelephonyCallEventRepository,
    TelephonyCallRepository,
    TelephonyMediaSessionRepository,
    TelephonyPhoneNumberRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import SecretMaterial, VaultClient
from nexus_ai.integrations.webhooks import decode_webhook_token
from nexus_ai.telephony.entities import (
    AccountStatus,
    CallDirection,
    CallLeg,
    CallLegRole,
    CallParticipant,
    CallState,
    MediaDirection,
    MediaState,
    NormalizedCallEvent,
    NormalizedInboundCall,
    ParticipantKind,
    RoutingContext,
    TelephonyAccount,
    TelephonyEventType,
)
from nexus_ai.telephony.errors import (
    TelephonyNotAuthorizedError,
    TelephonyWebhookInvalidError,
)
from nexus_ai.telephony.events import STATE_EVENT_TYPE
from nexus_ai.telephony.providers.base import WebhookContext
from nexus_ai.telephony.providers.registry import resolve_provider
from nexus_ai.telephony.state_machine import (
    FoldOutcome,
    fold_state,
    is_terminal,
    state_rank,
)


@dataclass(frozen=True, slots=True)
class WebhookAcceptance:
    accepted: bool
    processed: str
    challenge_response: str | None = None


class _Defer(Exception):
    """The referenced call is not visible yet — do not claim the event; let the provider
    retry."""


class InboundTelephonyService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._log = get_logger("nexus_ai.telephony.inbound")

    async def challenge(self, provider_key: str, token: str, ctx: WebhookContext) -> str | None:
        organization_id, account = await self._resolve_account(provider_key, token)
        del organization_id
        adapter = resolve_provider(account.provider)
        return adapter.webhook_challenge(account, ctx)

    async def receive(
        self, provider_key: str, token: str, ctx: WebhookContext
    ) -> WebhookAcceptance:
        if len(ctx.body) > self._settings.telephony.max_webhook_body_bytes:
            raise TelephonyWebhookInvalidError("the callback body exceeds the size limit")
        organization_id, account = await self._resolve_account(provider_key, token)
        if account.status is not AccountStatus.ACTIVE:
            raise TelephonyNotAuthorizedError("the telephony account is disabled")

        adapter = resolve_provider(account.provider)
        secret = await self._resolve_secret(organization_id, account)
        adapter.verify_webhook(
            account,
            ctx,
            secret,
            tolerance_seconds=self._settings.telephony.webhook_timestamp_tolerance_seconds,
        )

        parsed = adapter.parse_webhook(account, ctx)
        if parsed.inbound_call is not None:
            processed = await self._ingest_inbound(organization_id, account, parsed.inbound_call)
            return WebhookAcceptance(accepted=True, processed=processed)

        outcomes: list[str] = []
        for event in parsed.events:
            outcomes.append(await self._process_event(organization_id, account, event))
        return WebhookAcceptance(
            accepted=True, processed=",".join(outcomes) if outcomes else "empty"
        )

    # -- resolution -------------------------------------------------------------

    async def _resolve_account(
        self, provider_key: str, token: str
    ) -> tuple[UUID, TelephonyAccount]:
        from nexus_ai.integrations.errors import WebhookEndpointNotFoundError

        try:
            organization_id = decode_webhook_token(token)
        except WebhookEndpointNotFoundError as exc:
            raise TelephonyWebhookInvalidError("the webhook token is invalid") from exc
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await TelephonyAccountRepository(tenant).by_webhook_token(token)
        if account is None or account.provider.value != provider_key:
            raise TelephonyWebhookInvalidError("the webhook token does not match an account")
        return organization_id, account

    async def _resolve_secret(
        self, organization_id: UUID, account: TelephonyAccount
    ) -> SecretMaterial | None:
        if account.credential_ref is None:
            return None
        return await self._vault.get_secret(organization_id, account.credential_ref)

    # -- inbound call ingestion ----------------------------------------------

    async def _ingest_inbound(
        self,
        organization_id: UUID,
        account: TelephonyAccount,
        inbound: NormalizedInboundCall,
    ) -> str:
        # The dialed number MUST resolve to an owned, inbound-enabled number on this
        # account, or the call is refused — tenancy never comes from the payload.
        async with self._db.tenant_transaction(organization_id) as tenant:
            number = await TelephonyPhoneNumberRepository(tenant).by_e164(inbound.dialed_number)
        if number is None or number.account_id != account.id or not number.inbound_enabled:
            raise TelephonyNotAuthorizedError(
                "the dialed number is not an inbound-enabled number on this account"
            )
        routing = RoutingContext(
            organization_id=organization_id,
            account_id=account.id,
            phone_number_id=number.id,
            dialed_number=inbound.dialed_number,
        )

        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                events = TelephonyCallEventRepository(tenant)
                calls = TelephonyCallRepository(tenant)
                try:
                    await events.record(
                        account_id=account.id,
                        call_id=None,
                        provider_event_id=inbound.provider_event_id,
                        provider_call_id=inbound.provider_call_id,
                        event_type="STATE",
                        outcome="CLAIMED",
                        applied_state=None,
                        detail={"kind": "inbound"},
                    )
                except IntegrityError:
                    return "replayed"

                existing = await calls.by_provider_call_id(
                    account.id, inbound.provider_call_id, for_update=True
                )
                if existing is not None:
                    return "duplicate"

                call = await calls.insert(
                    call_id=uuid.uuid7(),
                    account_id=account.id,
                    from_number_id=None,
                    direction=CallDirection.INBOUND,
                    state=CallState.RINGING,
                    state_rank=state_rank(CallState.RINGING),
                    provider=account.provider,
                    provider_call_id=inbound.provider_call_id,
                    from_address=inbound.caller.address,
                    to_address=inbound.dialed_number,
                    legs=[
                        CallLeg(role=CallLegRole.A_LEG, participant=inbound.caller),
                        CallLeg(
                            role=CallLegRole.B_LEG,
                            participant=CallParticipant(
                                kind=ParticipantKind.PSTN, address=inbound.dialed_number
                            ),
                        ),
                    ],
                    correlation_id=inbound.correlation_id,
                    idempotency_key=None,
                    provider_timestamp=inbound.provider_timestamp,
                    provider_sequence=None,
                    ringing_at=dt.datetime.now(dt.UTC),
                )
                await self._enqueue_state_event(tenant.session, organization_id, call, routing)
        except IntegrityError:
            return "replayed"
        return "created"

    # -- provider event folding --------------------------------------------

    async def _process_event(
        self,
        organization_id: UUID,
        account: TelephonyAccount,
        event: NormalizedCallEvent,
    ) -> str:
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                events = TelephonyCallEventRepository(tenant)
                calls = TelephonyCallRepository(tenant)
                media = TelephonyMediaSessionRepository(tenant)
                try:
                    await events.record(
                        account_id=account.id,
                        call_id=None,
                        provider_event_id=event.provider_event_id,
                        provider_call_id=event.provider_call_id,
                        event_type=event.event_type.value,
                        outcome="CLAIMED",
                        applied_state=None,
                        detail={"type": event.event_type.value},
                    )
                except IntegrityError:
                    return "replayed"

                call = await calls.by_provider_call_id(
                    account.id, event.provider_call_id, for_update=True
                )
                if call is None:
                    raise _Defer

                if event.event_type is TelephonyEventType.STATE:
                    return await self._fold_state_event(
                        tenant.session, organization_id, calls, call, event
                    )
                if event.event_type is TelephonyEventType.DTMF:
                    await self._enqueue_dtmf(tenant.session, organization_id, call, event)
                    return "dtmf"
                await self._fold_media_event(tenant.session, organization_id, media, call, event)
                return "media"
        except _Defer:
            return "deferred"
        except IntegrityError:
            return "replayed"

    async def _fold_state_event(
        self,
        session: Any,
        organization_id: UUID,
        calls: TelephonyCallRepository,
        call: Any,
        event: NormalizedCallEvent,
    ) -> str:
        assert event.state is not None  # noqa: S101
        result = fold_state(
            current=call.state,
            current_rank=call.state_rank,
            current_provider_ts=call.provider_timestamp,
            current_sequence=call.provider_sequence,
            proposed=event.state,
            proposed_provider_ts=event.provider_timestamp,
            proposed_sequence=event.provider_sequence,
        )
        if result.outcome is FoldOutcome.IGNORED:
            return f"ignored:{result.reason}"

        now = dt.datetime.now(dt.UTC)
        changes: dict[str, Any] = {
            "state": result.state.value,
            "state_rank": state_rank(result.state),
            "provider_timestamp": event.provider_timestamp or call.provider_timestamp,
        }
        if event.provider_sequence is not None:
            changes["provider_sequence"] = event.provider_sequence
        if result.disposition is not None:
            changes["disposition"] = result.disposition.value
        if result.state is CallState.RINGING and call.ringing_at is None:
            changes["ringing_at"] = now
        if result.state is CallState.ANSWERED and call.answered_at is None:
            changes["answered_at"] = now
        if is_terminal(result.state):
            changes["ended_at"] = now
        updated = await calls.apply(call.id, changes)
        assert updated is not None  # noqa: S101
        await self._enqueue_state_event(session, organization_id, updated, None)
        return f"applied:{result.state.value}"

    async def _fold_media_event(
        self,
        session: Any,
        organization_id: UUID,
        media: TelephonyMediaSessionRepository,
        call: Any,
        event: NormalizedCallEvent,
    ) -> None:
        assert event.media_state is not None  # noqa: S101
        direction = event.media_direction or MediaDirection.BIDIRECTIONAL
        existing = (
            None if event.bridge_id is None else await media.by_bridge(call.id, event.bridge_id)
        )
        now = dt.datetime.now(dt.UTC)
        if event.media_state is MediaState.ACTIVE:
            if existing is None:
                session_row = await media.insert(
                    call_id=call.id,
                    direction=direction,
                    state=MediaState.ACTIVE,
                    bridge_id=event.bridge_id,
                    stream_id=event.stream_id,
                    started_at=now,
                )
                await self._enqueue_media_event(
                    session, organization_id, call, session_row.id, direction, started=True
                )
            return
        if event.media_state is MediaState.STOPPED and existing is not None:
            if existing.state is MediaState.STOPPED:
                return
            await media.apply(existing.id, {"state": MediaState.STOPPED.value, "stopped_at": now})
            await self._enqueue_media_event(
                session, organization_id, call, existing.id, direction, started=False
            )

    # -- events -----------------------------------------------------------

    async def _enqueue_state_event(
        self,
        session: Any,
        organization_id: UUID,
        call: Any,
        routing: RoutingContext | None,
    ) -> None:
        event_type = STATE_EVENT_TYPE.get(call.state.value)
        if event_type is None:
            return
        ctx = current_context()
        payload: dict[str, Any] = {
            "call_id": str(call.id),
            "account_id": str(call.account_id),
            "direction": call.direction.value,
            "provider": call.provider.value,
            "state": call.state.value,
            "provider_call_id": call.provider_call_id,
            "correlation_id": call.correlation_id,
        }
        if call.disposition is not None:
            payload["disposition"] = call.disposition.value
        if getattr(call, "error_code", None) and call.state is CallState.FAILED:
            payload["error_code"] = call.error_code
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="telephony_call",
                aggregate_id=str(call.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=None if ctx is None else ctx.correlation_id,
                payload=payload,
            ),
        )

    async def _enqueue_dtmf(
        self, session: Any, organization_id: UUID, call: Any, event: NormalizedCallEvent
    ) -> None:
        assert event.digit is not None  # noqa: S101
        ctx = current_context()
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type="telephony.dtmf.received",
                event_version=1,
                aggregate_type="telephony_call",
                aggregate_id=str(call.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=None if ctx is None else ctx.correlation_id,
                payload={
                    "call_id": str(call.id),
                    "account_id": str(call.account_id),
                    "digit": event.digit.value,
                    "correlation_id": call.correlation_id,
                },
            ),
        )

    async def _enqueue_media_event(
        self,
        session: Any,
        organization_id: UUID,
        call: Any,
        media_session_id: uuid.UUID,
        direction: MediaDirection,
        *,
        started: bool,
    ) -> None:
        ctx = current_context()
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type="telephony.media.started" if started else "telephony.media.stopped",
                event_version=1,
                aggregate_type="telephony_call",
                aggregate_id=str(call.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=None if ctx is None else ctx.correlation_id,
                payload={
                    "call_id": str(call.id),
                    "account_id": str(call.account_id),
                    "media_session_id": str(media_session_id),
                    "direction": direction.value,
                    "correlation_id": call.correlation_id,
                },
            ),
        )
