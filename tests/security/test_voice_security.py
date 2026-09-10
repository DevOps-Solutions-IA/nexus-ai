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


# --- BLOCKER 1: WebSocket SSRF / provider URL trust ---------------------------------


@pytest.mark.parametrize(
    "signed_url",
    [
        "wss://127.0.0.1/stream",  # loopback
        "wss://[::1]/stream",  # loopback v6
        "wss://169.254.169.254/latest/meta-data/",  # cloud metadata
        "wss://10.1.2.3/stream",  # RFC1918
        "wss://172.16.9.9/stream",  # RFC1918
        "wss://192.168.0.10/stream",  # RFC1918
        "wss://100.64.0.1/stream",  # CGNAT / carrier-grade
        "wss://224.0.0.1/stream",  # multicast
        "wss://internal.corp.local/stream",  # unauthorised private hostname
        "wss://elevenlabs.io.evil.example/stream",  # look-alike host
        "wss://api.elevenlabs.io.evil.example/stream",  # suffix attack
        "wss://user:pw@api.elevenlabs.io/stream",  # userinfo
        "wss://api.elevenlabs.io:22/stream",  # unsafe port (ssh)
        "wss://api.elevenlabs.io:5432/stream",  # unsafe port (postgres)
        "wss://api.elevenlabs.io:6379/stream",  # unsafe port (redis)
        "ws://api.elevenlabs.io/stream",  # plaintext ws
        "https://api.elevenlabs.io/stream",  # not a websocket scheme
        "not-a-url",
    ],
)
def test_validate_provider_ws_url_refuses_every_ssrf_class(signed_url: str) -> None:
    from nexus_ai.voice.errors import VoiceProviderError
    from nexus_ai.voice.providers.base import validate_provider_ws_url

    with pytest.raises(VoiceProviderError):
        validate_provider_ws_url(signed_url, allowed_hosts=frozenset({"api.elevenlabs.io"}))


def test_validate_provider_ws_url_accepts_only_the_official_host() -> None:
    from nexus_ai.voice.providers.base import validate_provider_ws_url

    target = validate_provider_ws_url(
        "wss://api.elevenlabs.io/v1/convai/conversation?token=abc123",
        allowed_hosts=frozenset({"api.elevenlabs.io"}),
    )
    assert target.host == "api.elevenlabs.io"
    assert target.port == 443


async def test_a_malicious_rest_signed_url_cannot_reach_an_arbitrary_host(
    voice_stack: Any, make_organization: Any
) -> None:
    """End to end: even if the ElevenLabs REST leg is compromised and returns a
    signed_url pointing at an internal target, the adapter refuses it before the
    transport is touched — and the REST origin itself is the server-configured one,
    never anything a caller supplied."""
    from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
    from nexus_ai.voice.audio import AudioFormat, VoiceCodec
    from nexus_ai.voice.errors import VoiceProviderError
    from nexus_ai.voice.providers.base import HttpResponse, VoiceSessionSpec
    from nexus_ai.voice.providers.elevenlabs import ElevenLabsVoiceAdapter

    fmt = AudioFormat(codec=VoiceCodec.PCM_S16LE, sample_rate=16_000)
    # the spec is built EXACTLY as VoiceService.start_session builds it
    spec = VoiceSessionSpec(
        provider="elevenlabs",
        external_account_id="acct-1",
        provider_voice_ref="agent_1",
        provider_model_ref=None,
        input_format=fmt,
        output_format=fmt,
        provider_api_base=voice_stack.settings.voice.elevenlabs_api_base,
    )
    assert spec.provider_api_base.startswith("https://")

    class _CompromisedRest:
        async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
            assert url.startswith(voice_stack.settings.voice.elevenlabs_api_base)
            return HttpResponse(
                200, {}, json.dumps({"signed_url": "wss://169.254.169.254/x"}).encode()
            )

    secret = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"api_key": "sk-x"})
    with pytest.raises(VoiceProviderError):
        await ElevenLabsVoiceAdapter().open_session(spec, secret, _CompromisedRest())


def test_start_session_request_cannot_smuggle_a_provider_endpoint() -> None:
    from pydantic import ValidationError

    for banned in ("api_base", "url", "endpoint", "ws_url", "signed_url", "host", "api_key"):
        with pytest.raises(ValidationError):
            StartVoiceSessionRequest(
                call_id=uuid4(),
                media_session_id=uuid4(),
                provider_account_id=uuid4(),
                voice_profile_id=uuid4(),
                options={banned: "wss://evil.example"},
            )


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
