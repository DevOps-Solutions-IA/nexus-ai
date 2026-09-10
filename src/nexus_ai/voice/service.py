"""The governed voice control pipeline (NXS-P12: NXS-VOICE-001 / NXS-EL-001).

Provider account + voice profile management, and the real-time voice-session lifecycle
attached to an ACTIVE NXS-P11 media session. NXS-P11 stays authoritative for call state,
ownership, numbers, routing, hangup and DTMF — this service never creates an alternative
source of truth for a call. Session start is idempotent (a canonical request
fingerprint, the same rigour as the NXS-P11 outbound-call fingerprint); a concurrent
duplicate start is exactly one logical session and one provider-side creation.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.telephony.repository import (
    TelephonyCallRepository,
    TelephonyMediaSessionRepository,
)
from nexus_ai.domain.voice.repository import (
    VoiceProfileRepository,
    VoiceProviderAccountRepository,
    VoiceSessionRepository,
    VoiceUsageRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial, VaultClient
from nexus_ai.integrations.webhooks import build_webhook_token
from nexus_ai.telephony.entities import CallDirection, MediaState
from nexus_ai.voice.account_config import validate_voice_account_configuration
from nexus_ai.voice.audio import normalize_audio_format
from nexus_ai.voice.bridge import MediaBridgePlan, plan_media_bridge
from nexus_ai.voice.entities import (
    ConfirmHandoffRequest,
    CreateVoiceAccountRequest,
    CreateVoiceProfileRequest,
    RequestHandoffRequest,
    StartVoiceSessionRequest,
    StopVoiceSessionRequest,
    UpdateVoiceProfileRequest,
    VoiceAccountStatus,
    VoiceHandoffState,
    VoiceProfile,
    VoiceProfileStatus,
    VoiceProvider,
    VoiceProviderAccount,
    VoiceProviderEvent,
    VoiceProviderEventKind,
    VoiceSession,
    VoiceSessionContext,
    VoiceSessionDirection,
)
from nexus_ai.voice.errors import (
    VoiceAccountNotFoundError,
    VoiceConfigInvalidError,
    VoiceIdempotencyConflictError,
    VoiceInvalidStateError,
    VoiceMediaNotReadyError,
    VoiceNotAuthorizedError,
    VoiceProfileNotFoundError,
    VoiceSessionNotFoundError,
)
from nexus_ai.voice.events import SESSION_STATE_EVENT_TYPE
from nexus_ai.voice.idempotency import voice_session_fingerprint
from nexus_ai.voice.media import GatewayMediaChannel
from nexus_ai.voice.providers.base import VoiceHttpTransport, VoiceSessionSpec
from nexus_ai.voice.providers.registry import resolve_voice_provider
from nexus_ai.voice.runtime import MediaChannel, RuntimeOutcome, VoiceSessionRuntime
from nexus_ai.voice.state_machine import (
    VoiceSessionDisposition,
    VoiceSessionState,
    fold_session_state,
    is_terminal,
    session_rank,
)
from nexus_ai.voice.transport import VoiceStreamTransport, WebsocketVoiceStreamTransport

_ENDING = VoiceSessionState.ENDING
_IDEMPOTENCY_RESOLVE_ATTEMPTS = 8

MediaChannelFactory = Callable[[MediaBridgePlan, VoiceSessionContext], MediaChannel]
TransportFactory = Callable[[], VoiceStreamTransport]


@dataclass(frozen=True, slots=True)
class _SessionClaim:
    session: VoiceSession
    is_owner: bool


def _default_media_channel(plan: MediaBridgePlan, ctx: VoiceSessionContext) -> MediaChannel:
    del ctx
    return GatewayMediaChannel(plan)


class VoiceService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
        http_transport: VoiceHttpTransport,
        *,
        transport_factory: TransportFactory | None = None,
        media_channel_factory: MediaChannelFactory | None = None,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._http = http_transport
        self._runtime = VoiceSessionRuntime(settings.voice)
        self._transport_factory: TransportFactory = transport_factory or (
            lambda: WebsocketVoiceStreamTransport(
                max_message_bytes=settings.voice.max_message_bytes
            )
        )
        self._media_channel_factory: MediaChannelFactory = (
            media_channel_factory or _default_media_channel
        )
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        #: Serializes the cancel + terminal transition for one session so concurrent
        #: stop / handoff callers never race on ``task.cancel()`` / a terminal write.
        self._terminalize_locks: dict[UUID, asyncio.Lock] = {}
        self._log = get_logger("nexus_ai.voice")

    # -- accounts -----------------------------------------------------------------

    async def create_account(
        self, organization_id: UUID, request: CreateVoiceAccountRequest
    ) -> VoiceProviderAccount:
        configuration = validate_voice_account_configuration(
            request.provider, request.configuration, settings=self._settings.voice
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            try:
                return await VoiceProviderAccountRepository(tenant).insert(
                    provider=request.provider,
                    slug=request.slug,
                    external_account_id=request.external_account_id,
                    configuration=configuration,
                    webhook_token=build_webhook_token(organization_id),
                )
            except IntegrityError as exc:
                raise VoiceConfigInvalidError(
                    "a voice provider account with this slug / provider identity already exists"
                ) from exc

    async def list_accounts(
        self, organization_id: UUID, *, limit: int
    ) -> list[VoiceProviderAccount]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await VoiceProviderAccountRepository(tenant).list_all(limit=limit)

    async def get_account(self, organization_id: UUID, account_id: UUID) -> VoiceProviderAccount:
        return await self._require_account(organization_id, account_id)

    async def update_account(
        self, organization_id: UUID, account_id: UUID, configuration: dict[str, Any]
    ) -> VoiceProviderAccount:
        account = await self._require_account(organization_id, account_id)
        clean = validate_voice_account_configuration(
            account.provider, configuration, settings=self._settings.voice
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await VoiceProviderAccountRepository(tenant).apply(
                account_id, {"configuration": clean}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def set_account_status(
        self, organization_id: UUID, account_id: UUID, status: VoiceAccountStatus
    ) -> VoiceProviderAccount:
        await self._require_account(organization_id, account_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await VoiceProviderAccountRepository(tenant).apply(
                account_id, {"status": status.value}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def store_account_credential(
        self, organization_id: UUID, account_id: UUID, fields: dict[str, str]
    ) -> None:
        account = await self._require_account(organization_id, account_id)
        ref = account.credential_ref or f"voice_{account_id.hex}"
        await self._vault.store_secret(
            organization_id,
            ref,
            SecretMaterial(CredentialType.PROVIDER_SECRET_SET, dict(fields)),
        )
        if account.credential_ref is None:
            async with self._db.tenant_transaction(organization_id) as tenant:
                await VoiceProviderAccountRepository(tenant).apply(
                    account_id, {"credential_ref": ref}
                )

    async def delete_account(self, organization_id: UUID, account_id: UUID) -> None:
        account = await self._require_account(organization_id, account_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            deleted = await VoiceProviderAccountRepository(tenant).delete_one(account_id)
        if not deleted:
            raise VoiceAccountNotFoundError("the voice provider account does not exist")
        if account.credential_ref is not None:
            await self._vault.delete_secret(organization_id, account.credential_ref)

    # -- profiles ---------------------------------------------------------------

    async def create_profile(
        self, organization_id: UUID, request: CreateVoiceProfileRequest
    ) -> VoiceProfile:
        await self._require_account(organization_id, request.account_id)
        input_format = normalize_audio_format(
            codec=request.input_format.codec,
            sample_rate=request.input_format.sample_rate,
            channels=request.input_format.channels,
            frame_ms=request.input_format.frame_ms,
        )
        output_format = normalize_audio_format(
            codec=request.output_format.codec,
            sample_rate=request.output_format.sample_rate,
            channels=request.output_format.channels,
            frame_ms=request.output_format.frame_ms,
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            try:
                return await VoiceProfileRepository(tenant).insert(
                    account_id=request.account_id,
                    slug=request.slug,
                    display_name=request.display_name,
                    provider_voice_ref=request.provider_voice_ref,
                    provider_model_ref=request.provider_model_ref,
                    input_format=input_format,
                    output_format=output_format,
                    config=dict(request.config),
                )
            except IntegrityError as exc:
                raise VoiceConfigInvalidError(
                    "a voice profile with this slug already exists"
                ) from exc

    async def list_profiles(self, organization_id: UUID, *, limit: int) -> list[VoiceProfile]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await VoiceProfileRepository(tenant).list_all(limit=limit)

    async def get_profile(self, organization_id: UUID, profile_id: UUID) -> VoiceProfile:
        return await self._require_profile(organization_id, profile_id)

    async def update_profile(
        self, organization_id: UUID, profile_id: UUID, request: UpdateVoiceProfileRequest
    ) -> VoiceProfile:
        await self._require_profile(organization_id, profile_id)
        changes: dict[str, Any] = {}
        if request.display_name is not None:
            changes["display_name"] = request.display_name
        if request.provider_voice_ref is not None:
            changes["provider_voice_ref"] = request.provider_voice_ref
        if request.provider_model_ref is not None:
            changes["provider_model_ref"] = request.provider_model_ref
        if request.config is not None:
            changes["config"] = dict(request.config)
        if not changes:
            return await self._require_profile(organization_id, profile_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await VoiceProfileRepository(tenant).apply(profile_id, changes)
        assert updated is not None  # noqa: S101
        return updated

    async def set_profile_status(
        self, organization_id: UUID, profile_id: UUID, status: VoiceProfileStatus
    ) -> VoiceProfile:
        await self._require_profile(organization_id, profile_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await VoiceProfileRepository(tenant).apply(
                profile_id, {"status": status.value}
            )
        assert updated is not None  # noqa: S101
        return updated

    # -- sessions -------------------------------------------------------------

    async def start_session(
        self, organization_id: UUID, request: StartVoiceSessionRequest
    ) -> VoiceSession:
        config = self._settings.voice
        if not config.enabled:
            raise VoiceConfigInvalidError("voice services are disabled")

        account = await self._require_account(organization_id, request.provider_account_id)
        if account.status is not VoiceAccountStatus.ACTIVE:
            raise VoiceConfigInvalidError("the voice provider account is disabled")
        profile = await self._require_profile(organization_id, request.voice_profile_id)
        if profile.status is not VoiceProfileStatus.ACTIVE:
            raise VoiceConfigInvalidError("the voice profile is disabled")
        if profile.account_id != account.id:
            raise VoiceConfigInvalidError(
                "the voice profile belongs to a different provider account"
            )

        call, media = await self._resolve_media(
            organization_id, request.call_id, request.media_session_id
        )
        direction = (
            VoiceSessionDirection.INBOUND
            if call.direction is CallDirection.INBOUND
            else VoiceSessionDirection.OUTBOUND
        )
        bridge_plan = plan_media_bridge(
            bridge_id=media.bridge_id,
            configuration=account.configuration,
            audio_format=profile.output_format,
        )

        fingerprint = voice_session_fingerprint(
            media_session_id=request.media_session_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
            options=request.options,
        )
        if request.idempotency_key is not None:
            replay = await self._replay(organization_id, request.idempotency_key, fingerprint)
            if replay is not None:
                return replay

        session_id = uuid.uuid7()
        claim = await self._persist_created(
            organization_id,
            session_id,
            account,
            profile,
            call.id,
            media.id,
            direction,
            request,
            fingerprint,
        )
        if not claim.is_owner:
            return claim.session

        ctx = VoiceSessionContext(
            organization_id=organization_id,
            session_id=session_id,
            call_id=call.id,
            media_session_id=media.id,
            account_id=account.id,
            direction=direction,
            input_format=profile.input_format,
            output_format=profile.output_format,
            correlation_id=request.correlation_id,
        )
        spec = VoiceSessionSpec(
            provider=account.provider.value,
            external_account_id=account.external_account_id,
            provider_voice_ref=profile.provider_voice_ref,
            provider_model_ref=profile.provider_model_ref,
            input_format=profile.input_format,
            output_format=profile.output_format,
            # Trusted provider REST origin from SERVER config only — never a request.
            provider_api_base=self._settings.voice.elevenlabs_api_base,
            options=dict(request.options),
            correlation_id=request.correlation_id,
        )
        secret = await self._resolve_secret(organization_id, account)
        connecting = await self._transition(
            organization_id, session_id, VoiceSessionState.CONNECTING
        )

        task = asyncio.create_task(
            self._run_session(ctx, spec, account.provider, bridge_plan, secret)
        )
        self._tasks[session_id] = task
        task.add_done_callback(self._forget_task)
        return connecting or claim.session

    def _forget_task(self, task: asyncio.Task[None]) -> None:
        for session_id, tracked in list(self._tasks.items()):
            if tracked is task:
                self._tasks.pop(session_id, None)

    async def get_session(self, organization_id: UUID, session_id: UUID) -> VoiceSession:
        return await self._require_session(organization_id, session_id)

    async def list_sessions(
        self,
        organization_id: UUID,
        *,
        account_id: UUID | None,
        call_id: UUID | None,
        limit: int,
    ) -> list[VoiceSession]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await VoiceSessionRepository(tenant).list_all(
                account_id=account_id, call_id=call_id, limit=limit
            )

    async def session_usage(self, organization_id: UUID, session_id: UUID) -> VoiceSession:
        return await self._require_session(organization_id, session_id)

    async def stop_session(
        self, organization_id: UUID, session_id: UUID, request: StopVoiceSessionRequest
    ) -> VoiceSession:
        del request
        return await self._cancel_and_terminalize(organization_id, session_id)

    async def _cancel_and_terminalize(
        self, organization_id: UUID, session_id: UUID
    ) -> VoiceSession:
        """Cancel the background runtime task (awaiting it so the transport / media
        channel are closed) and move the session to a CANCELLED terminal state in this
        service's own tenant transaction. Idempotent for an already-terminal session, and
        serialized per session so concurrent stop / handoff callers never race on
        ``task.cancel()`` or the terminal write."""
        lock = self._terminalize_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            task = self._tasks.get(session_id)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            async with self._db.tenant_transaction(organization_id) as tenant:
                repo = VoiceSessionRepository(tenant)
                session = await repo.by_id(session_id, for_update=True)
                if session is None:
                    raise VoiceSessionNotFoundError("no such voice session in this Organization")
                if is_terminal(session.state):
                    return session
                updated = await repo.apply(
                    session_id,
                    {
                        "state": VoiceSessionState.CANCELLED.value,
                        "state_rank": session_rank(VoiceSessionState.CANCELLED),
                        "disposition": VoiceSessionDisposition.CANCELLED.value,
                        "ended_at": dt.datetime.now(dt.UTC),
                    },
                )
                assert updated is not None  # noqa: S101
                await self._enqueue_state_event(tenant.session, organization_id, updated)
        return updated

    async def request_handoff(
        self, organization_id: UUID, session_id: UUID, request: RequestHandoffRequest
    ) -> VoiceSession:
        """Request an AI -> human handoff. Moves ``handoff_state`` AI -> PENDING_HUMAN,
        emits ``voice.handoff.requested`` and detaches the AI voice stream. It does NOT
        emit ``voice.handoff.completed`` and does NOT claim HUMAN. This is the entire
        public handoff surface (RBAC ``voice:use``). The PENDING_HUMAN -> HUMAN transition
        is *not* reachable from any request: it needs authoritative telephony evidence
        that a human bridge is live, which is a future NXS-P17 (Human Agent Operations)
        responsibility — see :meth:`confirm_handoff` (a non-public in-process seam).
        Idempotent; the initial transition rejects an already-terminal session."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = VoiceSessionRepository(tenant)
            session = await repo.by_id(session_id, for_update=True)
            if session is None:
                raise VoiceSessionNotFoundError("no such voice session in this Organization")
            if session.handoff_state is not VoiceHandoffState.AI:
                # already PENDING_HUMAN or HUMAN — idempotent. No duplicate event, no
                # second cancel + terminal write to race; the now-detached AI stream is
                # expected. Report the row as read under the lock.
                return session
            if is_terminal(session.state):
                raise VoiceInvalidStateError("the voice session has already ended")
            moved = await repo.apply(
                session_id, {"handoff_state": VoiceHandoffState.PENDING_HUMAN.value}
            )
            assert moved is not None  # noqa: S101
            await self._enqueue_named_event(
                tenant.session,
                organization_id,
                "voice.handoff.requested",
                moved,
                target=request.target,
            )
        # Only the caller that actually moved AI -> PENDING_HUMAN gets here: detach the AI
        # voice stream and end the voice session (NXS-P11 owns the real bridge to the
        # human). The handoff_state (PENDING_HUMAN) is preserved by _cancel_and_terminalize.
        await self._cancel_and_terminalize(organization_id, session_id)
        return await self._require_session(organization_id, session_id)

    async def confirm_handoff(
        self, organization_id: UUID, session_id: UUID, request: ConfirmHandoffRequest
    ) -> VoiceSession:
        """NON-PUBLIC in-process seam. Records an AUTHORITATIVE confirmation that a human
        bridge is live: moves ``handoff_state`` PENDING_HUMAN -> HUMAN and emits
        ``voice.handoff.completed``.

        P12 wires NO route to this and grants NO ``voice:*`` permission that reaches it.
        The ``HUMAN`` fact is telephony truth P12 cannot itself verify, so it must only be
        asserted by a trusted in-process authority — the future NXS-P17 (Human Agent
        Operations) service — AFTER that authority has verified the bridge against its own
        server-side state. ``request`` carries the bridge reference that authority has
        already validated; this method records it and does not re-derive trust from it.

        Tenant-scoped (only a session in ``organization_id``); rejects any state other
        than PENDING_HUMAN (``NXS_VOICE_INVALID_STATE``); ``HUMAN`` is idempotent."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = VoiceSessionRepository(tenant)
            session = await repo.by_id(session_id, for_update=True)
            if session is None:
                raise VoiceSessionNotFoundError("no such voice session in this Organization")
            if session.handoff_state is VoiceHandoffState.HUMAN:
                return session  # idempotent
            if session.handoff_state is not VoiceHandoffState.PENDING_HUMAN:
                raise VoiceInvalidStateError(
                    "the voice session is not awaiting a human bridge confirmation"
                )
            updated = await repo.apply(session_id, {"handoff_state": VoiceHandoffState.HUMAN.value})
            assert updated is not None  # noqa: S101
            await self._enqueue_named_event(
                tenant.session,
                organization_id,
                "voice.handoff.completed",
                updated,
                target=request.target,
                bridge_reference=request.bridge_reference,
            )
        return updated

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._terminalize_locks.clear()

    async def join(self, session_id: UUID) -> None:
        """Await the background runtime task for a session (used by tests)."""
        task = self._tasks.get(session_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # -- session runtime --------------------------------------------------------

    async def _run_session(
        self,
        ctx: VoiceSessionContext,
        spec: VoiceSessionSpec,
        provider: VoiceProvider,
        bridge_plan: MediaBridgePlan,
        secret: SecretMaterial | None,
    ) -> None:
        adapter = resolve_voice_provider(provider)
        transport = self._transport_factory()
        media = self._media_channel_factory(bridge_plan, ctx)

        async def _on_connected() -> None:
            await self._transition(ctx.organization_id, ctx.session_id, VoiceSessionState.CONNECTED)

        async def _on_event(event: VoiceProviderEvent) -> None:
            await self._handle_stream_event(ctx, event)

        outcome: RuntimeOutcome
        try:
            outcome = await self._runtime.run(
                ctx=ctx,
                spec=spec,
                adapter=adapter,
                transport=transport,
                media=media,
                http=self._http,
                secret=secret,
                on_event=_on_event,
                on_connected=_on_connected,
            )
        except asyncio.CancelledError:
            # The terminal transition + connection cleanup for a cancelled session are
            # the CALLER's job (stop_session / request_handoff hold their own tenant
            # transaction). Doing DB work here would run inside a cancelled coroutine
            # and can leak a pooled connection.
            raise
        except NxsError as exc:
            await self._finalize(
                ctx,
                RuntimeOutcome(disposition=VoiceSessionDisposition.FAILED, error_code=exc.code),
            )
            return
        await self._finalize(ctx, outcome)

    async def _handle_stream_event(
        self, ctx: VoiceSessionContext, event: VoiceProviderEvent
    ) -> None:
        if event.kind is VoiceProviderEventKind.SESSION_STARTED:
            await self._transition(
                ctx.organization_id,
                ctx.session_id,
                VoiceSessionState.STREAMING,
                provider_session_id=event.provider_session_id,
            )
            return
        if event.kind in (
            VoiceProviderEventKind.TRANSCRIPT,
            VoiceProviderEventKind.AGENT_TEXT,
        ):
            name = "voice.transcript.final" if event.is_final else "voice.transcript.partial"
            role = event.role.value if event.role is not None else "USER"
            async with self._db.tenant_transaction(ctx.organization_id) as tenant:
                await self._publisher.enqueue(
                    tenant.session,
                    EventEnvelope.create(
                        event_type=name,
                        event_version=1,
                        aggregate_type="voice_session",
                        aggregate_id=str(ctx.session_id),
                        producer=self._settings.service_name,
                        organization_id=ctx.organization_id,
                        correlation_id=ctx.correlation_id,
                        payload={
                            "session_id": str(ctx.session_id),
                            "call_id": str(ctx.call_id),
                            "role": role,
                            "char_count": len(event.text or ""),
                            "correlation_id": ctx.correlation_id,
                        },
                    ),
                )
            return
        if event.kind is VoiceProviderEventKind.INTERRUPTION:
            async with self._db.tenant_transaction(ctx.organization_id) as tenant:
                for name in ("voice.interruption.started", "voice.interruption.completed"):
                    await self._publisher.enqueue(
                        tenant.session,
                        EventEnvelope.create(
                            event_type=name,
                            event_version=1,
                            aggregate_type="voice_session",
                            aggregate_id=str(ctx.session_id),
                            producer=self._settings.service_name,
                            organization_id=ctx.organization_id,
                            correlation_id=ctx.correlation_id,
                            payload={
                                "session_id": str(ctx.session_id),
                                "call_id": str(ctx.call_id),
                                "correlation_id": ctx.correlation_id,
                            },
                        ),
                    )

    async def _finalize(self, ctx: VoiceSessionContext, outcome: RuntimeOutcome) -> None:
        terminal = {
            VoiceSessionDisposition.COMPLETED: VoiceSessionState.COMPLETED,
            VoiceSessionDisposition.CANCELLED: VoiceSessionState.CANCELLED,
            VoiceSessionDisposition.FAILED: VoiceSessionState.FAILED,
            VoiceSessionDisposition.UNKNOWN: VoiceSessionState.FAILED,
        }[outcome.disposition]
        async with self._db.tenant_transaction(ctx.organization_id) as tenant:
            repo = VoiceSessionRepository(tenant)
            session = await repo.by_id(ctx.session_id, for_update=True)
            if session is None or is_terminal(session.state):
                return
            changes: dict[str, Any] = {
                "state": terminal.value,
                "state_rank": session_rank(terminal),
                "disposition": outcome.disposition.value,
                "ended_at": dt.datetime.now(dt.UTC),
            }
            if outcome.provider_session_id and session.provider_session_id is None:
                changes["provider_session_id"] = outcome.provider_session_id[:200]
            if outcome.error_code is not None:
                changes["error_code"] = outcome.error_code
            if outcome.negotiated_format is not None:
                changes["negotiated_format"] = outcome.negotiated_format.model_dump(mode="json")
            changes["latency"] = outcome.latency.model_dump(mode="json")
            changes["usage"] = outcome.usage.model_dump(mode="json")
            updated = await repo.apply(ctx.session_id, changes)
            assert updated is not None  # noqa: S101
            await self._enqueue_state_event(tenant.session, ctx.organization_id, updated)
            await VoiceUsageRepository(tenant).upsert(
                session_id=ctx.session_id, usage=outcome.usage, latency=outcome.latency
            )
            await self._publisher.enqueue(
                tenant.session,
                EventEnvelope.create(
                    event_type="voice.usage.recorded",
                    event_version=1,
                    aggregate_type="voice_session",
                    aggregate_id=str(ctx.session_id),
                    producer=self._settings.service_name,
                    organization_id=ctx.organization_id,
                    correlation_id=ctx.correlation_id,
                    payload={
                        "session_id": str(ctx.session_id),
                        "account_id": str(ctx.account_id),
                        "audio_seconds_in": outcome.usage.audio_seconds_in,
                        "audio_seconds_out": outcome.usage.audio_seconds_out,
                        "provider_characters": outcome.usage.provider_characters,
                        "interruptions": outcome.usage.interruptions,
                        "time_to_first_audio_ms": outcome.latency.time_to_first_audio_ms,
                        "session_duration_ms": outcome.latency.session_duration_ms,
                        "correlation_id": ctx.correlation_id,
                    },
                ),
            )

    # -- helpers -----------------------------------------------------------------

    async def _require_account(
        self, organization_id: UUID, account_id: UUID
    ) -> VoiceProviderAccount:
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await VoiceProviderAccountRepository(tenant).by_id(account_id)
        if account is None:
            raise VoiceAccountNotFoundError(
                "the voice provider account does not exist in this Organization"
            )
        return account

    async def _require_profile(self, organization_id: UUID, profile_id: UUID) -> VoiceProfile:
        async with self._db.tenant_transaction(organization_id) as tenant:
            profile = await VoiceProfileRepository(tenant).by_id(profile_id)
        if profile is None:
            raise VoiceProfileNotFoundError("the voice profile does not exist in this Organization")
        return profile

    async def _require_session(self, organization_id: UUID, session_id: UUID) -> VoiceSession:
        async with self._db.tenant_transaction(organization_id) as tenant:
            session = await VoiceSessionRepository(tenant).by_id(session_id)
        if session is None:
            raise VoiceSessionNotFoundError("no such voice session in this Organization")
        return session

    async def _resolve_media(
        self, organization_id: UUID, call_id: UUID, media_session_id: UUID
    ) -> tuple[Any, Any]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            call = await TelephonyCallRepository(tenant).by_id(call_id)
            if call is None:
                raise VoiceNotAuthorizedError("the call does not exist in this Organization")
            sessions = await TelephonyMediaSessionRepository(tenant).by_call(call_id)
        media = next((m for m in sessions if m.id == media_session_id), None)
        if media is None:
            raise VoiceMediaNotReadyError(
                "the media session does not belong to this call in this Organization"
            )
        if media.state is not MediaState.ACTIVE:
            raise VoiceMediaNotReadyError(
                "the media session is not ACTIVE — a voice stream cannot be attached"
            )
        return call, media

    async def _resolve_secret(
        self, organization_id: UUID, account: VoiceProviderAccount
    ) -> SecretMaterial | None:
        if account.credential_ref is None:
            return None
        return await self._vault.get_secret(organization_id, account.credential_ref)

    async def _replay(
        self, organization_id: UUID, idempotency_key: str, fingerprint: str
    ) -> VoiceSession | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            existing = await VoiceSessionRepository(tenant).by_idempotency_key(idempotency_key)
        if existing is None:
            return None
        if existing.request_fingerprint != fingerprint:
            raise VoiceIdempotencyConflictError(
                "this idempotency key was already used for a semantically different session"
            )
        return existing

    async def _persist_created(
        self,
        organization_id: UUID,
        session_id: UUID,
        account: VoiceProviderAccount,
        profile: VoiceProfile,
        call_id: UUID,
        media_session_id: UUID,
        direction: VoiceSessionDirection,
        request: StartVoiceSessionRequest,
        fingerprint: str,
    ) -> _SessionClaim:
        stored_fingerprint = fingerprint if request.idempotency_key is not None else None
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                repo = VoiceSessionRepository(tenant)
                if request.idempotency_key is not None:
                    prior = await repo.by_idempotency_key(request.idempotency_key)
                    if prior is not None:
                        if prior.request_fingerprint != fingerprint:
                            raise VoiceIdempotencyConflictError(
                                "this idempotency key was already used for a "
                                "semantically different session"
                            )
                        return _SessionClaim(session=prior, is_owner=False)
                live = await repo.live_for_media_session(media_session_id, for_update=True)
                if live is not None:
                    if (
                        request.idempotency_key is not None
                        and live.idempotency_key == request.idempotency_key
                    ):
                        return _SessionClaim(session=live, is_owner=False)
                    raise VoiceMediaNotReadyError(
                        "a voice session is already live on this media session"
                    )
                session = await repo.insert(
                    session_id=session_id,
                    account_id=account.id,
                    voice_profile_id=profile.id,
                    call_id=call_id,
                    media_session_id=media_session_id,
                    direction=direction,
                    state=VoiceSessionState.PENDING,
                    state_rank=session_rank(VoiceSessionState.PENDING),
                    provider=account.provider,
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=stored_fingerprint,
                    correlation_id=request.correlation_id
                    or (None if (c := current_context()) is None else c.correlation_id),
                )
                await self._enqueue_state_event(tenant.session, organization_id, session)
        except IntegrityError as exc:
            if request.idempotency_key is not None:
                winner = await self._await_idempotency_winner(
                    organization_id, request.idempotency_key, fingerprint
                )
                if winner is not None:
                    return _SessionClaim(session=winner, is_owner=False)
            raise VoiceMediaNotReadyError(
                "a concurrent voice session creation conflicted on this media session"
            ) from exc
        return _SessionClaim(session=session, is_owner=True)

    async def _await_idempotency_winner(
        self, organization_id: UUID, idempotency_key: str, fingerprint: str
    ) -> VoiceSession | None:
        delay = 0.01
        for attempt in range(_IDEMPOTENCY_RESOLVE_ATTEMPTS):
            winner = await self._replay(organization_id, idempotency_key, fingerprint)
            if winner is not None:
                return winner
            if attempt + 1 < _IDEMPOTENCY_RESOLVE_ATTEMPTS:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.08)
        return None

    async def _transition(
        self,
        organization_id: UUID,
        session_id: UUID,
        target: VoiceSessionState,
        *,
        provider_session_id: str | None = None,
    ) -> VoiceSession | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = VoiceSessionRepository(tenant)
            session = await repo.by_id(session_id, for_update=True)
            if session is None or is_terminal(session.state):
                return session
            result = fold_session_state(
                current=session.state,
                current_rank=session.state_rank,
                current_provider_ts=session.provider_timestamp,
                current_sequence=session.provider_sequence,
                proposed=target,
            )
            if result.outcome.name == "IGNORED":
                return session
            changes: dict[str, Any] = {
                "state": result.state.value,
                "state_rank": session_rank(result.state),
            }
            if result.state is VoiceSessionState.CONNECTING and session.connecting_at is None:
                changes["connecting_at"] = dt.datetime.now(dt.UTC)
            if result.state is VoiceSessionState.CONNECTED and session.connected_at is None:
                changes["connected_at"] = dt.datetime.now(dt.UTC)
            if provider_session_id and session.provider_session_id is None:
                changes["provider_session_id"] = provider_session_id[:200]
            updated = await repo.apply(session_id, changes)
            assert updated is not None  # noqa: S101
            await self._enqueue_state_event(tenant.session, organization_id, updated)
        return updated

    async def _enqueue_state_event(
        self, session: Any, organization_id: UUID, row: VoiceSession
    ) -> None:
        event_type = SESSION_STATE_EVENT_TYPE.get(row.state.value)
        if event_type is None:
            return
        payload: dict[str, Any] = {
            "session_id": str(row.id),
            "account_id": str(row.account_id),
            "call_id": str(row.call_id),
            "media_session_id": str(row.media_session_id),
            "direction": row.direction.value,
            "provider": row.provider.value,
            "state": row.state.value,
            "correlation_id": row.correlation_id,
        }
        if row.disposition is not None and row.state.value in (
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        ):
            payload["disposition"] = row.disposition.value
        if row.error_code is not None and row.state is VoiceSessionState.FAILED:
            payload["error_code"] = row.error_code
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="voice_session",
                aggregate_id=str(row.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=row.correlation_id,
                payload=payload,
            ),
        )

    async def _enqueue_named_event(
        self,
        session: Any,
        organization_id: UUID,
        name: str,
        row: VoiceSession,
        *,
        target: str,
        bridge_reference: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "session_id": str(row.id),
            "call_id": str(row.call_id),
            "target": target,
            "correlation_id": row.correlation_id,
        }
        if bridge_reference is not None:
            payload["bridge_reference"] = bridge_reference
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=name,
                event_version=1,
                aggregate_type="voice_session",
                aggregate_id=str(row.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=row.correlation_id,
                payload=payload,
            ),
        )
