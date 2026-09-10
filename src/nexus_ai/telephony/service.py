"""The governed telephony control pipeline (NXS-P11: NXS-TEL-001).

Account / number management + outbound call creation + hangup + DTMF. The Customer /
Conversation model is not involved — a call is a standalone tenant-owned aggregate.

Outbound: caller -> account + owned caller-ID number resolution -> destination
canonicalization (E.164 / internal alias, fail closed) -> durable idempotency claim ->
provider adapter (governed transport, bounded timeout) -> CREATED call + P04 event. An
ambiguous provider timeout after the create request is represented explicitly and a
second call is never placed under the same key.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.telephony.repository import (
    TelephonyAccountRepository,
    TelephonyCallRepository,
    TelephonyMediaSessionRepository,
    TelephonyPhoneNumberRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial, VaultClient
from nexus_ai.integrations.webhooks import build_webhook_token
from nexus_ai.telephony.account_config import validate_account_configuration
from nexus_ai.telephony.destinations import (
    DestinationKind,
    canonicalize_destination,
    canonicalize_e164,
    safe_display_name,
)
from nexus_ai.telephony.entities import (
    AccountStatus,
    Call,
    CallDirection,
    CallLeg,
    CallLegRole,
    CallParticipant,
    CallState,
    CreateAccountRequest,
    CreateCallRequest,
    DtmfResult,
    HangupCallRequest,
    ParticipantKind,
    PhoneNumber,
    RegisterPhoneNumberRequest,
    SendDtmfRequest,
    TelephonyAccount,
)
from nexus_ai.telephony.errors import (
    TelephonyAccountNotFoundError,
    TelephonyCallNotFoundError,
    TelephonyConfigInvalidError,
    TelephonyDtmfInvalidError,
    TelephonyIdempotencyConflictError,
    TelephonyInvalidStateError,
    TelephonyNumberNotFoundError,
    TelephonyProviderTimeoutError,
)
from nexus_ai.telephony.events import STATE_EVENT_TYPE
from nexus_ai.telephony.idempotency import outbound_call_fingerprint
from nexus_ai.telephony.providers.base import OutboundCallSpec, TelephonyTransport
from nexus_ai.telephony.providers.registry import resolve_provider
from nexus_ai.telephony.state_machine import is_terminal, state_rank

_HANGUP_TARGET = CallState.ENDING


class AmbiguousProviderTimeoutError(NxsError):
    """The provider control call timed out AFTER the create request was sent — the call
    may or may not exist. Represented explicitly; a second call is NEVER placed."""

    code = "NXS_TELEPHONY_PROVIDER_TIMEOUT"
    status = 504
    title = "Telephony Provider Timeout"
    retryable = False


@dataclass(frozen=True, slots=True)
class _CallClaim:
    call: Call
    is_owner: bool


class TelephonyService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
        transport: TelephonyTransport,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._transport = transport
        self._log = get_logger("nexus_ai.telephony")

    # -- accounts --------------------------------------------------------------

    async def create_account(
        self, organization_id: UUID, request: CreateAccountRequest
    ) -> TelephonyAccount:
        configuration = validate_account_configuration(
            request.provider, request.configuration, settings=self._settings.telephony
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            try:
                return await TelephonyAccountRepository(tenant).insert(
                    provider=request.provider,
                    slug=request.slug,
                    external_account_id=request.external_account_id,
                    configuration=configuration,
                    webhook_token=build_webhook_token(organization_id),
                )
            except IntegrityError as exc:
                raise TelephonyConfigInvalidError(
                    "a telephony account with this slug / provider identity already exists"
                ) from exc

    async def list_accounts(self, organization_id: UUID, *, limit: int) -> list[TelephonyAccount]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await TelephonyAccountRepository(tenant).list_all(limit=limit)

    async def get_account(self, organization_id: UUID, account_id: UUID) -> TelephonyAccount:
        account = await self._require_account(organization_id, account_id)
        return account

    async def update_account(
        self, organization_id: UUID, account_id: UUID, configuration: dict[str, Any]
    ) -> TelephonyAccount:
        account = await self._require_account(organization_id, account_id)
        clean = validate_account_configuration(
            account.provider, configuration, settings=self._settings.telephony
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await TelephonyAccountRepository(tenant).apply(
                account_id, {"configuration": clean}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def set_account_status(
        self, organization_id: UUID, account_id: UUID, status: AccountStatus
    ) -> TelephonyAccount:
        await self._require_account(organization_id, account_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await TelephonyAccountRepository(tenant).apply(
                account_id, {"status": status.value}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def store_account_credential(
        self, organization_id: UUID, account_id: UUID, fields: dict[str, str]
    ) -> None:
        account = await self._require_account(organization_id, account_id)
        ref = account.credential_ref or f"telephony_{account_id.hex}"
        await self._vault.store_secret(
            organization_id,
            ref,
            SecretMaterial(CredentialType.PROVIDER_SECRET_SET, dict(fields)),
        )
        if account.credential_ref is None:
            async with self._db.tenant_transaction(organization_id) as tenant:
                await TelephonyAccountRepository(tenant).apply(account_id, {"credential_ref": ref})

    async def delete_account(self, organization_id: UUID, account_id: UUID) -> None:
        account = await self._require_account(organization_id, account_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            deleted = await TelephonyAccountRepository(tenant).delete_one(account_id)
        if not deleted:
            raise TelephonyAccountNotFoundError("the telephony account does not exist")
        if account.credential_ref is not None:
            await self._vault.delete_secret(organization_id, account.credential_ref)

    # -- phone numbers -------------------------------------------------------

    async def register_number(
        self, organization_id: UUID, request: RegisterPhoneNumberRequest
    ) -> PhoneNumber:
        await self._require_account(organization_id, request.account_id)
        e164 = canonicalize_e164(
            request.e164, default_country=self._settings.telephony.default_country
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            try:
                return await TelephonyPhoneNumberRepository(tenant).insert(
                    account_id=request.account_id,
                    e164=e164,
                    display_name=safe_display_name(request.display_name),
                    inbound_enabled=request.inbound_enabled,
                    verified=False,
                )
            except IntegrityError as exc:
                raise TelephonyConfigInvalidError(
                    "this phone number is already registered"
                ) from exc

    async def set_number_verified(
        self, organization_id: UUID, number_id: UUID, *, verified: bool
    ) -> PhoneNumber:
        await self._require_number(organization_id, number_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await TelephonyPhoneNumberRepository(tenant).apply(
                number_id, {"verified": verified}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def list_numbers(self, organization_id: UUID, *, limit: int) -> list[PhoneNumber]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await TelephonyPhoneNumberRepository(tenant).list_all(limit=limit)

    async def get_number(self, organization_id: UUID, number_id: UUID) -> PhoneNumber:
        return await self._require_number(organization_id, number_id)

    # -- calls ------------------------------------------------------------------

    async def create_call(
        self, organization_id: UUID, actor_user_id: UUID | None, request: CreateCallRequest
    ) -> Call:
        config = self._settings.telephony
        if not config.enabled:
            raise TelephonyConfigInvalidError("telephony services are disabled")
        account = await self._require_account(organization_id, request.provider_account_id)
        if account.status is not AccountStatus.ACTIVE:
            raise TelephonyConfigInvalidError("the telephony account is disabled")
        number = await self._require_number(organization_id, request.from_number_id)
        if number.account_id != account.id:
            raise TelephonyConfigInvalidError(
                "the caller-ID number belongs to a different telephony account"
            )
        if not number.verified:
            raise TelephonyConfigInvalidError(
                "the caller-ID number is not verified for outbound calling"
            )

        default_country = str(
            account.configuration.get("default_country") or config.default_country
        )
        destination = canonicalize_destination(request.destination, default_country=default_country)
        to_address = destination.value

        # The canonical fingerprint of every field that defines this logical call. All
        # four idempotency paths below compare exactly this value — a re-used key with a
        # different caller ID / destination / account / metadata is a deterministic
        # conflict, never a silent replay onto the original call.
        fingerprint = outbound_call_fingerprint(
            provider_account_id=account.id,
            from_number_id=number.id,
            destination=to_address,
            metadata=request.metadata,
        )

        if request.idempotency_key is not None:
            replay = await self._replay(organization_id, request.idempotency_key, fingerprint)
            if replay is not None:
                return replay

        call_id = uuid.uuid7()
        claim = await self._persist_created(
            organization_id, call_id, account, number, destination, to_address, request, fingerprint
        )
        if not claim.is_owner:
            return claim.call

        secret = await self._resolve_secret(organization_id, account)
        adapter = resolve_provider(account.provider)
        spec = OutboundCallSpec(
            account=account,
            caller_id_e164=number.e164,
            caller_display_name=number.display_name,
            destination_kind=destination.kind.value,
            destination_value=to_address,
            correlation_id=request.correlation_id,
            metadata=dict(request.metadata),
        )
        try:
            async with asyncio.timeout(config.provider_timeout_seconds):
                result = await adapter.create_outbound_call(spec, secret, self._transport)
        except (TimeoutError, TelephonyProviderTimeoutError) as exc:
            # A timeout AFTER the create request was dispatched is AMBIGUOUS — the
            # provider may already be ringing the call. Represent it explicitly and
            # NEVER place a second call under this idempotency key.
            await self._mark_ambiguous(organization_id, call_id)
            raise AmbiguousProviderTimeoutError(
                "the call creation request timed out; the call state is ambiguous and "
                "will not be retried under this idempotency key"
            ) from exc
        except NxsError:
            await self._mark_failed(organization_id, call_id)
            raise
        return await self._link_provider_call(organization_id, call_id, result.provider_call_id)

    async def hangup_call(
        self, organization_id: UUID, call_id: UUID, request: HangupCallRequest
    ) -> Call:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = TelephonyCallRepository(tenant)
            call = await repo.by_id(call_id, for_update=True)
            if call is None:
                raise TelephonyCallNotFoundError("no such call in this Organization")
            if is_terminal(call.state):
                return call  # idempotent — a hung-up call stays hung up
            account = await TelephonyAccountRepository(tenant).by_id(call.account_id)
            provider_call_id = call.provider_call_id
            updated = await repo.apply(
                call_id,
                {
                    "state": _HANGUP_TARGET.value,
                    "state_rank": state_rank(_HANGUP_TARGET),
                    "error_code": None,
                },
            )
            assert updated is not None  # noqa: S101
            await self._enqueue_state_event(tenant.session, organization_id, updated)

        # Best-effort provider hangup outside the state transaction: a provider outage
        # must not block the local terminal transition or corrupt state.
        if provider_call_id is not None and account is not None:
            secret = await self._resolve_secret(organization_id, account)
            adapter = resolve_provider(account.provider)
            try:
                async with asyncio.timeout(self._settings.telephony.provider_timeout_seconds):
                    await adapter.hangup_call(account, provider_call_id, secret, self._transport)
            except (TimeoutError, NxsError) as exc:
                self._log.warning(
                    "telephony_provider_hangup_failed",
                    call_id=str(call_id),
                    error=type(exc).__name__,
                )
        return updated

    async def send_dtmf(
        self, organization_id: UUID, call_id: UUID, request: SendDtmfRequest
    ) -> DtmfResult:
        if len(request.digits) > self._settings.telephony.max_dtmf_sequence_length:
            raise TelephonyDtmfInvalidError("the DTMF sequence is too long")
        call = await self._require_call(organization_id, call_id)
        if call.state not in (CallState.ANSWERED, CallState.BRIDGED):
            raise TelephonyInvalidStateError("DTMF can only be sent on an answered or bridged call")
        account = await self._require_account(organization_id, call.account_id)
        if call.provider_call_id is None:
            raise TelephonyInvalidStateError("the call has no provider channel yet")
        secret = await self._resolve_secret(organization_id, account)
        adapter = resolve_provider(account.provider)
        async with asyncio.timeout(self._settings.telephony.provider_timeout_seconds):
            await adapter.send_dtmf(
                account, call.provider_call_id, request.digits, secret, self._transport
            )
        return DtmfResult(call_id=call_id, digits=request.digits, accepted=True)

    async def get_call(self, organization_id: UUID, call_id: UUID) -> Call:
        return await self._require_call(organization_id, call_id)

    async def list_calls(
        self, organization_id: UUID, *, account_id: UUID | None, limit: int
    ) -> list[Call]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await TelephonyCallRepository(tenant).list_all(
                account_id=account_id, limit=limit
            )

    async def call_media_sessions(self, organization_id: UUID, call_id: UUID) -> list[Any]:
        await self._require_call(organization_id, call_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await TelephonyMediaSessionRepository(tenant).by_call(call_id)

    # -- helpers -------------------------------------------------------------

    async def _require_account(self, organization_id: UUID, account_id: UUID) -> TelephonyAccount:
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await TelephonyAccountRepository(tenant).by_id(account_id)
        if account is None:
            raise TelephonyAccountNotFoundError(
                "the telephony account does not exist in this Organization"
            )
        return account

    async def _require_number(self, organization_id: UUID, number_id: UUID) -> PhoneNumber:
        async with self._db.tenant_transaction(organization_id) as tenant:
            number = await TelephonyPhoneNumberRepository(tenant).by_id(number_id)
        if number is None:
            raise TelephonyNumberNotFoundError(
                "the phone number does not exist in this Organization"
            )
        return number

    async def _require_call(self, organization_id: UUID, call_id: UUID) -> Call:
        async with self._db.tenant_transaction(organization_id) as tenant:
            call = await TelephonyCallRepository(tenant).by_id(call_id)
        if call is None:
            raise TelephonyCallNotFoundError("no such call in this Organization")
        return call

    async def _resolve_secret(
        self, organization_id: UUID, account: TelephonyAccount
    ) -> SecretMaterial | None:
        if account.credential_ref is None:
            return None
        return await self._vault.get_secret(organization_id, account.credential_ref)

    async def _replay(
        self, organization_id: UUID, idempotency_key: str, fingerprint: str
    ) -> Call | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            existing = await TelephonyCallRepository(tenant).by_idempotency_key(idempotency_key)
        if existing is None:
            return None
        if existing.request_fingerprint != fingerprint:
            raise TelephonyIdempotencyConflictError(
                "this idempotency key was already used for a semantically different call"
            )
        return existing

    async def _persist_created(
        self,
        organization_id: UUID,
        call_id: UUID,
        account: TelephonyAccount,
        number: PhoneNumber,
        destination: Any,
        to_address: str,
        request: CreateCallRequest,
        fingerprint: str,
    ) -> _CallClaim:
        ctx = current_context()
        a_leg = CallLeg(
            role=CallLegRole.A_LEG,
            participant=CallParticipant(
                kind=ParticipantKind.PSTN, address=number.e164, display_name=number.display_name
            ),
        )
        b_kind = (
            ParticipantKind.PSTN
            if destination.kind is DestinationKind.PHONE
            else ParticipantKind.SIP_ENDPOINT
        )
        b_leg = CallLeg(
            role=CallLegRole.B_LEG,
            participant=CallParticipant(kind=b_kind, address=to_address),
        )
        # Only persisted (and compared) when the call is idempotent.
        stored_fingerprint = fingerprint if request.idempotency_key is not None else None
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                repo = TelephonyCallRepository(tenant)
                if request.idempotency_key is not None:
                    prior = await repo.by_idempotency_key(request.idempotency_key)
                    if prior is not None:
                        if prior.request_fingerprint != fingerprint:
                            raise TelephonyIdempotencyConflictError(
                                "this idempotency key was already used for a "
                                "semantically different call"
                            )
                        return _CallClaim(call=prior, is_owner=False)
                call = await repo.insert(
                    call_id=call_id,
                    account_id=account.id,
                    from_number_id=number.id,
                    direction=CallDirection.OUTBOUND,
                    state=CallState.CREATED,
                    state_rank=state_rank(CallState.CREATED),
                    provider=account.provider,
                    provider_call_id=None,
                    from_address=number.e164,
                    to_address=to_address,
                    legs=[a_leg, b_leg],
                    correlation_id=request.correlation_id
                    or (None if ctx is None else ctx.correlation_id),
                    idempotency_key=request.idempotency_key,
                    request_fingerprint=stored_fingerprint,
                    provider_timestamp=None,
                    provider_sequence=None,
                )
                await self._enqueue_state_event(tenant.session, organization_id, call)
        except IntegrityError as exc:
            if request.idempotency_key is not None:
                winner = await self._await_idempotency_winner(
                    organization_id, request.idempotency_key, fingerprint
                )
                if winner is not None:
                    return _CallClaim(call=winner, is_owner=False)
            raise TelephonyIdempotencyConflictError(
                "a concurrent call creation conflicted"
            ) from exc
        return _CallClaim(call=call, is_owner=True)

    async def _await_idempotency_winner(
        self, organization_id: UUID, idempotency_key: str, fingerprint: str
    ) -> Call | None:
        delay = 0.01
        for attempt in range(8):
            winner = await self._replay(organization_id, idempotency_key, fingerprint)
            if winner is not None:
                return winner
            if attempt < 7:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.08)
        return None

    async def _link_provider_call(
        self, organization_id: UUID, call_id: UUID, provider_call_id: str
    ) -> Call:
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await TelephonyCallRepository(tenant).apply(
                call_id, {"provider_call_id": provider_call_id[:200]}
            )
        assert updated is not None  # noqa: S101
        return updated

    async def _mark_ambiguous(self, organization_id: UUID, call_id: UUID) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = TelephonyCallRepository(tenant)
            current = await repo.by_id(call_id, for_update=True)
            if current is not None and not is_terminal(current.state):
                await repo.apply(call_id, {"error_code": "NXS_TELEPHONY_PROVIDER_TIMEOUT"})

    async def _mark_failed(self, organization_id: UUID, call_id: UUID) -> Call:
        now = dt.datetime.now(dt.UTC)
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = TelephonyCallRepository(tenant)
            current = await repo.by_id(call_id, for_update=True)
            if current is not None and is_terminal(current.state):
                return current
            updated = await repo.apply(
                call_id,
                {
                    "state": CallState.FAILED.value,
                    "state_rank": state_rank(CallState.FAILED),
                    "disposition": "FAILED",
                    "error_code": "NXS_TELEPHONY_PROVIDER_ERROR",
                    "ended_at": now,
                },
            )
            assert updated is not None  # noqa: S101
            await self._enqueue_state_event(tenant.session, organization_id, updated)
        return updated

    async def _enqueue_state_event(self, session: Any, organization_id: UUID, call: Call) -> None:
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
        if call.error_code is not None and call.state is CallState.FAILED:
            payload["error_code"] = call.error_code
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="telephony_call",
            aggregate_id=str(call.id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)
