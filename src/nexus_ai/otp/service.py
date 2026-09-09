"""The governed OTP issuance + verification pipeline (NXS-P10: NXS-OTP-001).

Issue:  caller -> purpose + account + destination validation -> durable throttle /
cooldown -> CSPRNG code -> keyed hash + challenge row (one active per subject/purpose,
DB-enforced) -> P04 ``otp.challenge.issued`` -> delivery through the NXS-P09 messaging
service -> safe result (never the code).

Verify: caller -> tenant scope -> ``SELECT ... FOR UPDATE`` challenge -> expiry / used /
revoked / locked checks -> attempt policy -> constant-time hash compare -> atomic
terminal transition -> P04 audit event -> result.

This service never opens a socket, never speaks a provider protocol and never holds a
provider credential — delivery is orchestrated entirely through ``MessagingService``.
The Customer / Conversation model is NXS-P06's; this service resolves and attaches.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import IdentityNormalizationError, NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.customers.entities import IdentityType
from nexus_ai.domain.customers.normalization import normalize_identity_value
from nexus_ai.domain.customers.service import ConversationService, CustomerService
from nexus_ai.domain.otp.repository import OtpChallengeRepository
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.messaging.entities import (
    AccountStatus,
    MessageChannel,
    MessageContent,
    MessageContentType,
    OutboundAddressInput,
    SendMessageRequest,
)
from nexus_ai.messaging.errors import (
    MessagingAccountNotFoundError,
    MessagingRateLimitedError,
    MessagingTimeoutError,
)
from nexus_ai.messaging.service import (
    MessagingService,
    resolve_inbound_conversation,
    resolve_inbound_customer,
)
from nexus_ai.otp.codes import (
    canonical_context,
    destination_fingerprint,
    generate_code,
    hash_code,
    mask_destination,
    verify_code,
)
from nexus_ai.otp.entities import (
    CURRENT_HASH_VERSION,
    IssueOtpRequest,
    IssueOtpResult,
    OtpChallenge,
    OtpChallengeCreate,
    OtpChallengeView,
    OtpChannel,
    OtpDeliveryStatus,
    OtpStatus,
    OtpSubjectType,
    OtpVerifyOutcome,
    VerifyOtpRequest,
    VerifyOtpResult,
)
from nexus_ai.otp.errors import (
    OtpAlreadyUsedError,
    OtpChallengeNotFoundError,
    OtpConfigInvalidError,
    OtpDeliveryFailedError,
    OtpExpiredError,
    OtpIdempotencyConflictError,
    OtpInvalidError,
    OtpLockedError,
    OtpRateLimitedError,
    OtpResendTooSoonError,
)
from nexus_ai.otp.events import (
    OTP_EVENT_DELIVERY_FAILED,
    OTP_EVENT_EXPIRED,
    OTP_EVENT_FAILED_ATTEMPT,
    OTP_EVENT_ISSUED,
    OTP_EVENT_LOCKED,
    OTP_EVENT_RATE_LIMITED,
    OTP_EVENT_REVOKED,
    OTP_EVENT_VERIFIED,
)
from nexus_ai.otp.purposes import resolve_purpose

_CHANNEL_TO_MESSAGE: dict[OtpChannel, MessageChannel] = {
    OtpChannel.SMS: MessageChannel.SMS,
    OtpChannel.EMAIL: MessageChannel.EMAIL,
}
_CHANNEL_TO_IDENTITY: dict[OtpChannel, IdentityType] = {
    OtpChannel.SMS: IdentityType.PHONE,
    OtpChannel.EMAIL: IdentityType.EMAIL,
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class _AmbiguousDelivery(Exception):
    """Internal: the provider call timed out — delivery is ambiguous."""


class OtpService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        messaging: MessagingService,
        customers: CustomerService,
        conversations: ConversationService,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._messaging = messaging
        self._customers = customers
        self._conversations = conversations
        self._log = get_logger("nexus_ai.otp")
        self._ephemeral_pepper = (
            secrets.token_hex(32) if settings.otp.allow_ephemeral_pepper else None
        )

    # -- public API ------------------------------------------------------------

    async def issue(self, organization_id: UUID, request: IssueOtpRequest) -> IssueOtpResult:
        config = self._settings.otp
        if not config.enabled:
            raise OtpConfigInvalidError("OTP services are disabled")
        purpose = resolve_purpose(request.purpose, request.channel)
        await self._require_account(organization_id, request)
        destination = self._normalize_destination(request)

        pepper = self._pepper()
        dest_fp = destination_fingerprint(pepper, request.channel.value, destination)
        request_fp = _request_fingerprint(
            purpose.key, request.channel.value, destination, request.messaging_account_id
        )

        if request.idempotency_key is not None:
            replay = await self._replay(organization_id, request.idempotency_key, request_fp)
            if replay is not None:
                return replay

        now = _utcnow()
        challenge_id = uuid.uuid7()
        code = generate_code(config.code_length)
        context = canonical_context(
            organization_id=organization_id,
            challenge_id=challenge_id,
            purpose=purpose.key,
            channel=request.channel.value,
            destination=destination,
            hash_version=CURRENT_HASH_VERSION,
        )
        create = OtpChallengeCreate(
            id=challenge_id,
            organization_id=organization_id,
            subject_type=OtpSubjectType.DESTINATION,
            destination=destination,
            destination_fingerprint=dest_fp,
            purpose=purpose.key,
            channel=request.channel,
            messaging_account_id=request.messaging_account_id,
            code_hash=hash_code(pepper, context, code),
            hash_version=CURRENT_HASH_VERSION,
            max_attempts=config.max_attempts,
            request_fingerprint=request_fp,
            idempotency_key=request.idempotency_key,
            correlation_id=request.correlation_id,
            issued_at=now,
            expires_at=now + dt.timedelta(seconds=config.ttl_seconds),
            resend_after=now + dt.timedelta(seconds=config.resend_cooldown_seconds),
        )

        challenge = await self._persist_issued(organization_id, create, dest_fp, purpose.key, now)
        delivery = await self._deliver_or_unwind(
            organization_id, challenge, code, config.ttl_seconds
        )
        return self._result(challenge, delivery, replayed=False)

    async def verify(
        self, organization_id: UUID, challenge_id: UUID, request: VerifyOtpRequest
    ) -> VerifyOtpResult:
        """Verify a submitted code. Every state transition (expiry materialization,
        attempt increment, lockout, terminal VERIFIED) is committed inside ONE
        ``SELECT ... FOR UPDATE`` transaction; the deterministic error — if any — is
        raised only AFTER that transaction commits, so a rejected attempt still counts.
        """
        if not self._settings.otp.enabled:
            raise OtpConfigInvalidError("OTP services are disabled")
        now = _utcnow()
        pepper = self._pepper()
        failure: NxsError | None = None
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = OtpChallengeRepository(tenant)
            challenge = await repo.by_id(challenge_id, for_update=True)
            if challenge is None:
                raise OtpChallengeNotFoundError("no such OTP challenge in this Organization")

            terminal = self._terminal_failure(challenge)
            if terminal is not None:
                return _raise(terminal)
            if now >= challenge.expires_at:
                await repo.apply(challenge_id, {"status": OtpStatus.EXPIRED.value})
                await self._enqueue_event(
                    tenant.session, organization_id, OTP_EVENT_EXPIRED, challenge
                )
                failure = OtpExpiredError("the OTP has expired")
            else:
                failure = await self._apply_attempt(
                    tenant, repo, organization_id, challenge, request.code, pepper, now
                )
                if failure is None:
                    return VerifyOtpResult(
                        challenge_id=challenge_id,
                        outcome=OtpVerifyOutcome.VERIFIED,
                        verified_at=now,
                    )
        return _raise(failure)

    async def _apply_attempt(
        self,
        tenant: Any,
        repo: OtpChallengeRepository,
        organization_id: UUID,
        challenge: OtpChallenge,
        code: str,
        pepper: bytes,
        now: dt.datetime,
    ) -> NxsError | None:
        context = canonical_context(
            organization_id=organization_id,
            challenge_id=challenge.id,
            purpose=challenge.purpose,
            channel=challenge.channel.value,
            destination=challenge.destination,
            hash_version=challenge.hash_version,
        )
        attempts = challenge.attempts + 1
        if verify_code(pepper, context, code, challenge.code_hash):
            updated = await repo.apply(
                challenge.id,
                {
                    "status": OtpStatus.VERIFIED.value,
                    "verified_at": now,
                    "attempts": attempts,
                    "last_attempt_at": now,
                },
            )
            assert updated is not None  # noqa: S101
            await self._enqueue_event(
                tenant.session, organization_id, OTP_EVENT_VERIFIED, updated, attempts=attempts
            )
            return None

        locked = attempts >= challenge.max_attempts
        changes: dict[str, Any] = {"attempts": attempts, "last_attempt_at": now}
        if locked:
            changes["status"] = OtpStatus.LOCKED.value
            changes["locked_at"] = now
        updated = await repo.apply(challenge.id, changes)
        assert updated is not None  # noqa: S101
        await self._enqueue_event(
            tenant.session,
            organization_id,
            OTP_EVENT_FAILED_ATTEMPT,
            updated,
            attempts=attempts,
            max_attempts=challenge.max_attempts,
        )
        if locked:
            await self._enqueue_event(
                tenant.session, organization_id, OTP_EVENT_LOCKED, updated, attempts=attempts
            )
            return OtpLockedError("too many incorrect attempts; the OTP challenge is locked")
        return OtpInvalidError("the code is not valid")

    async def resend(
        self, organization_id: UUID, challenge_id: UUID, *, correlation_id: str | None = None
    ) -> IssueOtpResult:
        """Re-issue for the subject/purpose/channel/account of an existing challenge. The
        normal cooldown, throttle and one-active-challenge policy all apply."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            original = await OtpChallengeRepository(tenant).by_id(challenge_id)
        if original is None:
            raise OtpChallengeNotFoundError("no such OTP challenge in this Organization")
        return await self.issue(
            organization_id,
            IssueOtpRequest(
                purpose=original.purpose,
                channel=original.channel,
                destination=original.destination,
                messaging_account_id=original.messaging_account_id,
                correlation_id=correlation_id or original.correlation_id,
            ),
        )

    async def get_challenge(self, organization_id: UUID, challenge_id: UUID) -> OtpChallengeView:
        async with self._db.tenant_transaction(organization_id) as tenant:
            challenge = await OtpChallengeRepository(tenant).by_id(challenge_id)
        if challenge is None:
            raise OtpChallengeNotFoundError("no such OTP challenge in this Organization")
        return challenge.public_view(
            masked_destination=mask_destination(challenge.channel.value, challenge.destination)
        )

    async def purge_expired(self, organization_id: UUID, *, older_than_seconds: int) -> int:
        """Cleanup seam (NXS-P15 owns scheduling). Materializes overdue ACTIVE challenges
        as EXPIRED, then deletes terminal challenges older than the retention window."""
        now = _utcnow()
        cutoff = now - dt.timedelta(seconds=max(older_than_seconds, 3600))
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await OtpChallengeRepository(tenant).purge_terminal_before(cutoff)

    # -- issuance helpers ----------------------------------------------------

    async def _require_account(self, organization_id: UUID, request: IssueOtpRequest) -> Any:
        try:
            account = await self._messaging.get_account(
                organization_id, request.messaging_account_id
            )
        except MessagingAccountNotFoundError as exc:
            raise OtpConfigInvalidError(
                "the messaging account does not exist in this Organization"
            ) from exc
        if account.channel.value != request.channel.value:
            raise OtpConfigInvalidError("the messaging account is on a different channel")
        if account.status is not AccountStatus.ACTIVE:
            raise OtpConfigInvalidError("the messaging account is disabled")
        return account

    def _normalize_destination(self, request: IssueOtpRequest) -> str:
        try:
            return normalize_identity_value(
                _CHANNEL_TO_IDENTITY[request.channel],
                request.destination,
                default_country=request.default_country,
            )
        except IdentityNormalizationError as exc:
            raise OtpConfigInvalidError(
                f"the destination is not a valid {request.channel.value.lower()} address"
            ) from exc

    async def _replay(
        self, organization_id: UUID, idempotency_key: str, request_fp: str
    ) -> IssueOtpResult | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            existing = await OtpChallengeRepository(tenant).by_idempotency_key(idempotency_key)
        if existing is None:
            return None
        if existing.request_fingerprint != request_fp:
            raise OtpIdempotencyConflictError(
                "this idempotency key was already used for a different OTP request"
            )
        return self._result(existing, OtpDeliveryStatus.SKIPPED, replayed=True)

    async def _persist_issued(
        self,
        organization_id: UUID,
        create: OtpChallengeCreate,
        dest_fp: str,
        purpose_key: str,
        now: dt.datetime,
    ) -> OtpChallenge:
        config = self._settings.otp
        window_start = now - dt.timedelta(seconds=config.issue_window_seconds)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                repo = OtpChallengeRepository(tenant)
                issued = await repo.count_issued_since(dest_fp, purpose_key, window_start)
                if issued >= config.max_issues_per_window:
                    raise OtpRateLimitedError(
                        "too many OTP issuances for this destination and purpose"
                    )
                active = await repo.active_for_subject(dest_fp, purpose_key, for_update=True)
                if active is not None:
                    if now < active.resend_after:
                        raise OtpResendTooSoonError(
                            "an OTP was issued recently; wait before requesting another"
                        )
                    revoked = await repo.apply(
                        active.id, {"status": OtpStatus.REVOKED.value, "revoked_at": now}
                    )
                    assert revoked is not None  # noqa: S101
                    await self._enqueue_event(
                        tenant.session,
                        organization_id,
                        OTP_EVENT_REVOKED,
                        revoked,
                        reason="superseded_by_new_issuance",
                    )
                challenge = await repo.insert(create)
                await self._enqueue_event(
                    tenant.session,
                    organization_id,
                    OTP_EVENT_ISSUED,
                    challenge,
                    expires_at=challenge.expires_at.isoformat(),
                    replayed=False,
                )
        except OtpRateLimitedError:
            await self._enqueue_standalone(
                organization_id, OTP_EVENT_RATE_LIMITED, create, reason="issue_window_exceeded"
            )
            raise
        except IntegrityError as exc:
            if create.idempotency_key is not None:
                replay = await self._replay(
                    organization_id, create.idempotency_key, create.request_fingerprint
                )
                if replay is not None:
                    return await self._reload(organization_id, replay.challenge_id)
            raise OtpResendTooSoonError(
                "another OTP for this destination and purpose was just issued"
            ) from exc
        return challenge

    async def _reload(self, organization_id: UUID, challenge_id: UUID) -> OtpChallenge:
        async with self._db.tenant_transaction(organization_id) as tenant:
            challenge = await OtpChallengeRepository(tenant).by_id(challenge_id)
        assert challenge is not None  # noqa: S101
        return challenge

    async def _deliver_or_unwind(
        self, organization_id: UUID, challenge: OtpChallenge, code: str, ttl_seconds: int
    ) -> OtpDeliveryStatus:
        # A replayed challenge (idempotency) never re-delivers.
        if challenge.delivery_message_id is not None:
            return OtpDeliveryStatus.SKIPPED
        try:
            message_id = await self._deliver(organization_id, challenge, code, ttl_seconds)
        except _AmbiguousDelivery:
            self._log.warning(
                "otp_delivery_ambiguous",
                challenge_id=str(challenge.id),
                channel=challenge.channel.value,
            )
            return OtpDeliveryStatus.UNCONFIRMED
        except NxsError as exc:
            await self._unwind_failed_delivery(organization_id, challenge, exc)
            raise OtpDeliveryFailedError(
                "the OTP could not be delivered", extensions={"delivery_error": exc.code}
            ) from exc
        async with self._db.tenant_transaction(organization_id) as tenant:
            await OtpChallengeRepository(tenant).apply(
                challenge.id, {"delivery_message_id": message_id}
            )
        return OtpDeliveryStatus.SENT

    async def _deliver(
        self, organization_id: UUID, challenge: OtpChallenge, code: str, ttl_seconds: int
    ) -> UUID:
        from nexus_ai.otp.templates import render_message

        account = await self._messaging.get_account(organization_id, challenge.messaging_account_id)
        message_channel = _CHANNEL_TO_MESSAGE[challenge.channel]
        masked = mask_destination(challenge.channel.value, challenge.destination)
        customer_id = await resolve_inbound_customer(
            self._customers, organization_id, message_channel, challenge.destination, masked
        )
        conversation = await resolve_inbound_conversation(
            self._conversations,
            organization_id,
            account,
            customer_id,
            # One OTP conversation per destination (not per purpose): all one-time-code
            # traffic to an address shares a single P06 timeline.
            f"otp:{challenge.destination_fingerprint}",
        )
        rendered = render_message(challenge.channel, code, ttl_seconds)
        send_request = SendMessageRequest(
            account_id=account.id,
            conversation_id=conversation.id,
            to=(OutboundAddressInput(value=challenge.destination),),
            content=MessageContent(content_type=MessageContentType.TEXT, text=rendered.text),
            subject=rendered.subject,
            idempotency_key=f"otp-{challenge.id.hex}",
            correlation_id=challenge.correlation_id,
        )
        try:
            message = await self._messaging.send(organization_id, None, send_request)
        except MessagingTimeoutError as exc:
            raise _AmbiguousDelivery from exc
        except MessagingRateLimitedError as exc:
            raise OtpDeliveryFailedError(
                "the messaging provider is rate limiting", extensions={"delivery_error": exc.code}
            ) from exc
        return message.id

    async def _unwind_failed_delivery(
        self, organization_id: UUID, challenge: OtpChallenge, error: NxsError
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = OtpChallengeRepository(tenant)
            current = await repo.by_id(challenge.id, for_update=True)
            if current is not None and current.status is OtpStatus.ACTIVE:
                await repo.apply(
                    challenge.id,
                    {"status": OtpStatus.REVOKED.value, "revoked_at": _utcnow()},
                )
            await self._enqueue_event(
                tenant.session,
                organization_id,
                OTP_EVENT_DELIVERY_FAILED,
                challenge,
                error_code=error.code,
            )

    # -- verification helpers ----------------------------------------------

    @staticmethod
    def _terminal_failure(challenge: OtpChallenge) -> NxsError | None:
        if challenge.status is OtpStatus.VERIFIED:
            return OtpAlreadyUsedError("this OTP was already used")
        if challenge.status is OtpStatus.LOCKED:
            return OtpLockedError("this OTP challenge is locked")
        if challenge.status is OtpStatus.EXPIRED:
            return OtpExpiredError("the OTP has expired")
        if challenge.status is OtpStatus.REVOKED:
            # Do not disclose that a challenge was revoked — it is simply not valid.
            return OtpInvalidError("the code is not valid")
        return None

    # -- events -----------------------------------------------------------

    async def _enqueue_event(
        self,
        session: Any,
        organization_id: UUID,
        event_type: str,
        challenge: OtpChallenge,
        **extra: Any,
    ) -> None:
        ctx = current_context()
        payload: dict[str, Any] = {
            "challenge_id": str(challenge.id),
            "purpose": challenge.purpose,
            "channel": challenge.channel.value,
            "masked_destination": mask_destination(challenge.channel.value, challenge.destination),
        }
        payload.update({key: value for key, value in extra.items() if value is not None})
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="otp_challenge",
            aggregate_id=str(challenge.id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=(challenge.correlation_id if ctx is None else ctx.correlation_id),
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)

    async def _enqueue_standalone(
        self, organization_id: UUID, event_type: str, create: OtpChallengeCreate, **extra: Any
    ) -> None:
        """Emit an audit event that must survive even though its issuance transaction
        rolled back (a rejected issuance leaves no challenge row)."""
        ctx = current_context()
        payload: dict[str, Any] = {
            "challenge_id": str(create.id),
            "purpose": create.purpose,
            "channel": create.channel.value,
            "masked_destination": mask_destination(create.channel.value, create.destination),
        }
        payload.update({key: value for key, value in extra.items() if value is not None})
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="otp_challenge",
            aggregate_id=str(create.id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=(create.correlation_id if ctx is None else ctx.correlation_id),
            payload=payload,
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._publisher.enqueue(tenant.session, envelope)

    # -- misc -------------------------------------------------------------

    def _pepper(self) -> bytes:
        configured = self._settings.otp.pepper
        if configured is not None:
            return configured.get_secret_value().encode("utf-8")
        if self._ephemeral_pepper is not None:
            return self._ephemeral_pepper.encode("utf-8")
        raise OtpConfigInvalidError("the OTP pepper is not configured")

    def _result(
        self, challenge: OtpChallenge, delivery: OtpDeliveryStatus, *, replayed: bool
    ) -> IssueOtpResult:
        return IssueOtpResult(
            challenge_id=challenge.id,
            status=challenge.status,
            purpose=challenge.purpose,
            channel=challenge.channel,
            masked_destination=mask_destination(challenge.channel.value, challenge.destination),
            expires_at=challenge.expires_at,
            resend_after=challenge.resend_after,
            max_attempts=challenge.max_attempts,
            delivery=delivery,
            replayed=replayed,
        )


def _raise(error: NxsError | None) -> NoReturn:
    assert error is not None  # noqa: S101
    raise error


def _request_fingerprint(purpose: str, channel: str, destination: str, account_id: UUID) -> str:
    canonical = json.dumps(
        {
            "purpose": purpose,
            "channel": channel,
            "destination": destination,
            "account_id": str(account_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
