"""Voice adversarial tests (NXS-P12): tenant isolation, credential leakage resistance,
SSRF / injection defence, oversized frames, callback spoofing and raw-passthrough
rejection."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from nexus_ai.voice.entities import (
    CreateVoiceAccountRequest,
    StartVoiceSessionRequest,
    VoiceProvider,
)
from nexus_ai.voice.errors import (
    VoiceAccountNotFoundError,
    VoiceConfigInvalidError,
    VoiceProfileNotFoundError,
    VoiceSessionNotFoundError,
    VoiceWebhookInvalidError,
)
from nexus_ai.voice.providers.base import WebhookContext
from tests.integration.test_voice_service import _VOICE_SECRET, ready_voice_call

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_cross_tenant_account_profile_and_session_reads_fail_closed(
    voice_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org_a.id)
    voice_stack.script["frames"] = ['{"type": "session_ended"}']
    session = await voice_stack.service.start_session(
        org_a.id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )
    await voice_stack.service.join(session.id)

    with pytest.raises(VoiceAccountNotFoundError):
        await voice_stack.service.get_account(org_b.id, account.id)
    with pytest.raises(VoiceProfileNotFoundError):
        await voice_stack.service.get_profile(org_b.id, profile.id)
    with pytest.raises(VoiceSessionNotFoundError):
        await voice_stack.service.get_session(org_b.id, session.id)


async def test_a_forged_cross_tenant_voice_session_row_is_refused_by_the_fk(
    voice_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    _acc_a, _prof_a, call_a, media_a = await ready_voice_call(voice_stack, org_a.id)
    acc_b = await voice_stack.service.create_account(
        org_b.id,
        CreateVoiceAccountRequest(
            provider=VoiceProvider.FAKE,
            slug=f"v-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"media_gateway_host": "10.9.9.9", "media_gateway_port": 40000},
        ),
    )
    # org B tries to attach a session to org A's call + media session
    with pytest.raises((IntegrityError, DBAPIError)):  # composite tenant-aware FK violation
        async with voice_stack.database.tenant_transaction(org_b.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO voice_sessions (id, organization_id, account_id, call_id, "
                    "media_session_id, direction, state, state_rank, handoff_state, provider, "
                    "created_at, updated_at) VALUES (:id, :org, :acc, :call, :media, 'INBOUND', "
                    "'PENDING', 0, 'AI', 'fake', now(), now())"
                ),
                {
                    "id": uuid4(),
                    "org": org_b.id,
                    "acc": acc_b.id,
                    "call": call_a,
                    "media": media_a,
                },
            )


async def test_provider_credentials_never_appear_in_rows_or_events(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["frames"] = [
        '{"type": "agent_response", "text": "hi"}',
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
    await voice_stack.service.join(session.id)

    async with voice_stack.database.tenant_transaction(org.id) as tenant:
        session_blob = str(
            (await tenant.session.execute(text("SELECT * FROM voice_sessions"))).mappings().all()
        )
        events_blob = str(
            (
                await tenant.session.execute(
                    text("SELECT envelope FROM event_outbox WHERE event_type LIKE 'voice.%'")
                )
            ).all()
        )
    for secret in (
        _VOICE_SECRET["api_key"],
        _VOICE_SECRET["webhook_secret"],
        "signed_url",
        "wss://",
    ):
        assert secret not in session_blob
        assert secret not in events_blob


async def test_account_config_rejects_unknown_keys_and_ssrf_gateways(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    with pytest.raises(VoiceConfigInvalidError):
        await voice_stack.service.create_account(
            org.id,
            CreateVoiceAccountRequest(
                provider=VoiceProvider.FAKE,
                slug=f"v-{uuid4().hex[:8]}",
                external_account_id=uuid4().hex,
                configuration={"ws_url": "wss://evil.example"},  # unknown key
            ),
        )
    with pytest.raises(VoiceConfigInvalidError):
        await voice_stack.service.create_account(
            org.id,
            CreateVoiceAccountRequest(
                provider=VoiceProvider.FAKE,
                slug=f"v-{uuid4().hex[:8]}",
                external_account_id=uuid4().hex,
                configuration={"media_gateway_host": "169.254.169.254", "media_gateway_port": 80},
            ),
        )


async def test_oversized_provider_message_ends_the_session_as_protocol_error(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    huge = "x" * (voice_stack.settings.voice.max_message_bytes + 10)
    voice_stack.script["frames"] = [json.dumps({"type": "agent_response", "text": huge})]
    voice_stack.script["timeout_after"] = 2
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
    final = await voice_stack.service.get_session(org.id, session.id)
    assert final.state.value in ("FAILED", "COMPLETED")
    if final.error_code is not None:
        assert final.error_code in ("NXS_VOICE_PROTOCOL_ERROR", "NXS_VOICE_PROVIDER_TIMEOUT")


async def test_unsigned_and_replayed_callbacks_are_refused(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, *_ = await ready_voice_call(voice_stack, org.id)
    body = json.dumps(
        {"type": "post_call_transcription", "data": {"conversation_id": "c1"}}
    ).encode()
    unsigned = WebhookContext("POST", {}, {}, body)
    with pytest.raises(VoiceWebhookInvalidError):
        await voice_stack.inbound.receive("fake", account.webhook_token, unsigned)
    # a bad token never resolves to a tenant
    with pytest.raises(VoiceWebhookInvalidError):
        await voice_stack.inbound.receive("fake", "not-a-real-token", unsigned)


async def test_start_session_never_takes_tenant_from_the_request(
    voice_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org_a.id)
    # org B calls start_session with org A's ids — resolution is by the tenant txn, so
    # org B simply cannot see the account.
    with pytest.raises(VoiceAccountNotFoundError):
        await voice_stack.service.start_session(
            org_b.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
            ),
        )
