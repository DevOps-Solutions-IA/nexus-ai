"""Voice account / profile / session lifecycle against the real DB + event outbox with a
fake provider and an ACTIVE NXS-P11 media session (NXS-P12: NXS-VOICE-001 / NXS-EL-001)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.telephony.entities import (
    CreateAccountRequest as TelCreateAccountRequest,
)
from nexus_ai.telephony.entities import (
    RegisterPhoneNumberRequest,
    TelephonyProvider,
)
from nexus_ai.telephony.providers.base import WebhookContext
from nexus_ai.voice.entities import (
    CreateVoiceAccountRequest,
    CreateVoiceProfileRequest,
    RequestHandoffRequest,
    StartVoiceSessionRequest,
    StopVoiceSessionRequest,
    VoiceAccountStatus,
    VoiceHandoffState,
    VoiceProvider,
    VoiceSessionState,
)
from nexus_ai.voice.errors import (
    VoiceConfigInvalidError,
    VoiceIdempotencyConflictError,
    VoiceInvalidStateError,
    VoiceMediaNotReadyError,
    VoiceSessionNotFoundError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_TEL_SECRET = {"webhook_secret": "tel-webhook-secret", "ari_user": "u", "ari_password": "p"}
_VOICE_SECRET = {"api_key": "sk-fake-elevenlabs", "webhook_secret": "voice-hook-secret"}
_PCM = {"codec": "pcm_16000", "sample_rate": 16_000, "channels": 1, "frame_ms": 20}
_GATEWAY = {"media_gateway_host": "10.20.30.40", "media_gateway_port": 40000}


def _tel_ctx(body: dict[str, Any]) -> WebhookContext:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"tel-webhook-secret", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": ts},
        query={},
        body=raw,
    )


async def ready_voice_call(
    stack: Any, org_id: Any, *, provider: VoiceProvider = VoiceProvider.FAKE
):
    """Provision a voice account + profile and an ACTIVE NXS-P11 inbound media session.
    Returns (account, profile, call_id, media_session_id)."""
    account = await stack.service.create_account(
        org_id,
        CreateVoiceAccountRequest(
            provider=provider,
            slug=f"voice-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration=dict(_GATEWAY),
        ),
    )
    await stack.service.store_account_credential(org_id, account.id, _VOICE_SECRET)
    account = await stack.service.get_account(org_id, account.id)
    profile = await stack.service.create_profile(
        org_id,
        CreateVoiceProfileRequest(
            account_id=account.id,
            slug=f"prof-{uuid4().hex[:8]}",
            display_name="Support Voice",
            provider_voice_ref="agent_abc123",
            input_format=_PCM,
            output_format=_PCM,
        ),
    )

    tel_account = await stack.telephony.create_account(
        org_id,
        TelCreateAccountRequest(
            provider=TelephonyProvider.FAKE,
            slug=f"tel-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"default_country": "1"},
        ),
    )
    await stack.telephony.store_account_credential(org_id, tel_account.id, _TEL_SECRET)
    tel_account = await stack.telephony.get_account(org_id, tel_account.id)
    e164 = f"+1415{uuid4().int % 10_000_000:07d}"
    dialed = await stack.telephony.register_number(
        org_id, RegisterPhoneNumberRequest(account_id=tel_account.id, e164=e164)
    )
    provider_call = f"pc-{uuid4().hex}"
    await stack.telephony_inbound.receive(
        "fake",
        tel_account.webhook_token,
        _tel_ctx(
            {
                "kind": "inbound",
                "event_id": uuid4().hex,
                "call_id": provider_call,
                "from": "+14155550142",
                "to": dialed.e164,
            }
        ),
    )
    for body in (
        {"event_id": uuid4().hex, "call_id": provider_call, "event": "ANSWERED"},
        {
            "event_id": uuid4().hex,
            "call_id": provider_call,
            "event": "MEDIA",
            "media_state": "ACTIVE",
            "bridge_id": f"asterisk-bridge-{uuid4().hex[:12]}",
            "stream_id": "st-1",
        },
    ):
        await stack.telephony_inbound.receive("fake", tel_account.webhook_token, _tel_ctx(body))

    async with stack.database.tenant_transaction(org_id) as tenant:
        call_id = (
            await tenant.session.execute(
                text("SELECT id FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).scalar_one()
        media_id = (
            await tenant.session.execute(
                text(
                    "SELECT id FROM telephony_media_sessions "
                    "WHERE call_id = :c AND state = 'ACTIVE'"
                ),
                {"c": call_id},
            )
        ).scalar_one()
    return account, profile, call_id, media_id


async def test_account_and_profile_lifecycle(voice_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account, profile, _call, _media = await ready_voice_call(voice_stack, org.id)
    assert account.public_view().receive_path.startswith("/api/v1/webhooks/voice/fake/")
    assert profile.input_format.codec.value == "pcm_s16le"

    disabled = await voice_stack.service.set_account_status(
        org.id, account.id, VoiceAccountStatus.DISABLED
    )
    assert disabled.status.value == "DISABLED"


async def test_start_session_happy_path_streams_and_completes(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["frames"] = [
        '{"type": "session_started", "session_id": "prov-sess-42"}',
        '{"type": "agent_response", "text": "Hello, how can I help?"}',
        '{"type": "session_ended"}',
    ]

    session = await voice_stack.service.start_session(
        org.id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )
    assert session.state is VoiceSessionState.CONNECTING
    await voice_stack.service.join(session.id)

    final = await voice_stack.service.get_session(org.id, session.id)
    assert final.state is VoiceSessionState.COMPLETED
    assert final.disposition is not None
    assert final.provider_session_id == "prov-sess-42"
    assert final.usage is not None and final.latency is not None
    assert final.negotiated_format is not None

    async with voice_stack.database.tenant_transaction(org.id) as tenant:
        events = {
            r.event_type
            for r in (
                await tenant.session.execute(
                    text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'voice.%'")
                )
            ).all()
        }
        usage_rows = (
            await tenant.session.execute(
                text("SELECT count(*) FROM voice_usage_records WHERE session_id = :s"),
                {"s": final.id},
            )
        ).scalar_one()
    assert "voice.session.created" in events
    assert "voice.session.connecting" in events
    assert "voice.session.completed" in events
    assert "voice.usage.recorded" in events
    assert "voice.transcript.final" in events
    assert usage_rows == 1
    # the transcript BODY is never persisted or evented — only a char count
    assert not any("Hello, how can I help" in e for e in events)


async def test_start_session_requires_active_media_session(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    async with voice_stack.database.tenant_transaction(org.id) as tenant:
        await tenant.session.execute(
            text("UPDATE telephony_media_sessions SET state = 'STOPPED' WHERE id = :i"),
            {"i": media_id},
        )
    with pytest.raises(VoiceMediaNotReadyError):
        await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
            ),
        )


async def test_start_session_idempotency_replays_and_conflicts(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["frames"] = ['{"type": "session_ended"}']
    request = StartVoiceSessionRequest(
        call_id=call_id,
        media_session_id=media_id,
        provider_account_id=account.id,
        voice_profile_id=profile.id,
        idempotency_key="voice-key-abc123",
        options={"language": "en"},
    )
    first = await voice_stack.service.start_session(org.id, request)
    replay = await voice_stack.service.start_session(org.id, request)
    assert replay.id == first.id

    with pytest.raises(VoiceIdempotencyConflictError):
        await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
                idempotency_key="voice-key-abc123",
                options={"language": "fr"},
            ),
        )
    await voice_stack.service.join(first.id)


async def test_one_live_session_per_media_session(voice_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["hold"] = True  # winner session stays live until stopped

    first = await voice_stack.service.start_session(
        org.id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )
    with pytest.raises(VoiceMediaNotReadyError):
        await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
            ),
        )
    await voice_stack.service.stop_session(org.id, first.id, StopVoiceSessionRequest())


async def test_stop_session_is_terminal_safe_and_idempotent(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["hold"] = True  # session stays live until stopped
    session = await voice_stack.service.start_session(
        org.id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )
    stopped = await voice_stack.service.stop_session(org.id, session.id, StopVoiceSessionRequest())
    assert stopped.state is VoiceSessionState.CANCELLED
    again = await voice_stack.service.stop_session(org.id, session.id, StopVoiceSessionRequest())
    assert again.state is VoiceSessionState.CANCELLED


async def _handoff_events(voice_stack: Any, org_id: Any) -> list[str]:
    async with voice_stack.database.tenant_transaction(org_id) as tenant:
        return [
            r.event_type
            for r in (
                await tenant.session.execute(
                    text(
                        "SELECT event_type FROM event_outbox "
                        "WHERE event_type LIKE 'voice.handoff%' ORDER BY created_at"
                    )
                )
            ).all()
        ]


async def _started_session(voice_stack: Any, org_id: Any) -> Any:
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org_id)
    voice_stack.script["hold"] = True
    return await voice_stack.service.start_session(
        org_id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )


async def test_request_handoff_moves_to_pending_human_not_completed(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    session = await _started_session(voice_stack, org.id)

    handed = await voice_stack.service.request_handoff(
        org.id, session.id, RequestHandoffRequest(target="human_agent")
    )
    # PENDING_HUMAN — never HUMAN — and the AI stream is detached (session terminal)
    assert handed.handoff_state is VoiceHandoffState.PENDING_HUMAN
    assert handed.state in (VoiceSessionState.CANCELLED, VoiceSessionState.FAILED)
    assert await _handoff_events(voice_stack, org.id) == ["voice.handoff.requested"]

    # repeated request is idempotent — no second requested event, still PENDING_HUMAN
    again = await voice_stack.service.request_handoff(
        org.id, session.id, RequestHandoffRequest(target="human_agent")
    )
    assert again.handoff_state is VoiceHandoffState.PENDING_HUMAN
    assert await _handoff_events(voice_stack, org.id) == ["voice.handoff.requested"]


async def test_confirm_handoff_is_the_only_path_to_human(
    voice_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.voice.entities import ConfirmHandoffRequest

    org = await make_organization()
    session = await _started_session(voice_stack, org.id)

    # cannot confirm before request — session is still AI
    with pytest.raises(VoiceInvalidStateError):
        await voice_stack.service.confirm_handoff(
            org.id, session.id, ConfirmHandoffRequest(bridge_reference="p11-leg-1")
        )

    await voice_stack.service.request_handoff(org.id, session.id, RequestHandoffRequest())
    confirmed = await voice_stack.service.confirm_handoff(
        org.id, session.id, ConfirmHandoffRequest(bridge_reference="p11-leg-1")
    )
    assert confirmed.handoff_state is VoiceHandoffState.HUMAN
    assert await _handoff_events(voice_stack, org.id) == [
        "voice.handoff.requested",
        "voice.handoff.completed",
    ]
    # the completed event carries the authoritative bridge reference for audit
    async with voice_stack.database.tenant_transaction(org.id) as tenant:
        completed = (
            await tenant.session.execute(
                text(
                    "SELECT envelope FROM event_outbox WHERE event_type = 'voice.handoff.completed'"
                )
            )
        ).scalar_one()
    assert completed["payload"]["bridge_reference"] == "p11-leg-1"
    # confirm is idempotent
    again = await voice_stack.service.confirm_handoff(
        org.id, session.id, ConfirmHandoffRequest(bridge_reference="p11-leg-1")
    )
    assert again.handoff_state is VoiceHandoffState.HUMAN
    assert (await _handoff_events(voice_stack, org.id)).count("voice.handoff.completed") == 1


async def test_confirm_handoff_cannot_cross_tenants(
    voice_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.voice.entities import ConfirmHandoffRequest

    org_a = await make_organization()
    org_b = await make_organization()
    session = await _started_session(voice_stack, org_a.id)
    await voice_stack.service.request_handoff(org_a.id, session.id, RequestHandoffRequest())

    with pytest.raises(VoiceSessionNotFoundError):
        await voice_stack.service.confirm_handoff(
            org_b.id, session.id, ConfirmHandoffRequest(bridge_reference="x")
        )


async def test_terminal_voice_session_rejects_handoff(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["frames"] = ['{"type": "session_ended"}']
    session = await voice_stack.service.start_session(
        org.id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )
    await voice_stack.service.join(session.id)
    with pytest.raises(VoiceInvalidStateError):
        await voice_stack.service.request_handoff(org.id, session.id, RequestHandoffRequest())


async def test_concurrent_handoff_requests_are_deterministic(
    voice_stack: Any, make_organization: Any
) -> None:
    import asyncio

    org = await make_organization()
    session = await _started_session(voice_stack, org.id)
    results = await asyncio.gather(
        *(
            voice_stack.service.request_handoff(org.id, session.id, RequestHandoffRequest())
            for _ in range(6)
        ),
        return_exceptions=True,
    )
    assert all(not isinstance(r, Exception) for r in results)
    assert {r.handoff_state for r in results} == {VoiceHandoffState.PENDING_HUMAN}
    assert await _handoff_events(voice_stack, org.id) == ["voice.handoff.requested"]


async def test_disabled_subsystem_refuses_start(voice_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.service._settings = voice_stack.settings.model_copy(
        update={"voice": voice_stack.settings.voice.model_copy(update={"enabled": False})}
    )
    with pytest.raises(VoiceConfigInvalidError):
        await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
            ),
        )


async def test_account_and_profile_management_endpoints(
    voice_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.voice.entities import (
        UpdateVoiceProfileRequest,
        VoiceProfileStatus,
    )

    org = await make_organization()
    account, profile, _call, _media = await ready_voice_call(voice_stack, org.id)

    updated = await voice_stack.service.update_account(
        org.id, account.id, {"media_gateway_host": "10.5.5.5", "media_gateway_port": 41000}
    )
    assert updated.configuration["media_gateway_host"] == "10.5.5.5"
    assert len(await voice_stack.service.list_accounts(org.id, limit=10)) == 1

    prof = await voice_stack.service.update_profile(
        org.id, profile.id, UpdateVoiceProfileRequest(display_name="Renamed")
    )
    assert prof.display_name == "Renamed"
    disabled = await voice_stack.service.set_profile_status(
        org.id, profile.id, VoiceProfileStatus.DISABLED
    )
    assert disabled.status is VoiceProfileStatus.DISABLED
    assert len(await voice_stack.service.list_profiles(org.id, limit=10)) == 1

    sessions = await voice_stack.service.list_sessions(
        org.id, account_id=account.id, call_id=None, limit=10
    )
    assert sessions == []


async def test_delete_account_without_profiles(voice_stack: Any, make_organization: Any) -> None:
    org = await make_organization()
    created = await voice_stack.service.create_account(
        org.id,
        CreateVoiceAccountRequest(
            provider=VoiceProvider.FAKE,
            slug=f"del-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"media_gateway_host": "10.7.7.7", "media_gateway_port": 40000},
        ),
    )
    await voice_stack.service.store_account_credential(org.id, created.id, _VOICE_SECRET)
    await voice_stack.service.delete_account(org.id, created.id)
    from nexus_ai.voice.errors import VoiceAccountNotFoundError

    with pytest.raises(VoiceAccountNotFoundError):
        await voice_stack.service.get_account(org.id, created.id)


async def test_unsupported_audio_format_is_rejected_at_profile_create(
    voice_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.voice.errors import VoiceUnsupportedAudioError

    org = await make_organization()
    account, _profile, _call, _media = await ready_voice_call(voice_stack, org.id)
    with pytest.raises(VoiceUnsupportedAudioError):
        await voice_stack.service.create_profile(
            org.id,
            CreateVoiceProfileRequest(
                account_id=account.id,
                slug=f"bad-{uuid4().hex[:8]}",
                display_name="Bad",
                provider_voice_ref="agent_x",
                input_format={"codec": "opus", "sample_rate": 48_000},
                output_format={"codec": "pcm_16000", "sample_rate": 16_000},
            ),
        )
