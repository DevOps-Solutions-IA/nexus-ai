"""The ElevenLabs voice adapter — serializes / parses the documented Conversational AI
real-time contract behind the Nexus-neutral interface, and never leaks a provider type
or credential (NXS-P12, ADR-0087). CONTRACT-CERTIFIED against canned frames."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import pytest

from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
from nexus_ai.voice.audio import AudioFormat, VoiceCodec
from nexus_ai.voice.entities import VoiceProviderEventKind
from nexus_ai.voice.errors import (
    VoiceNotAuthorizedError,
    VoiceProtocolError,
    VoiceProviderError,
    VoiceProviderTimeoutError,
    VoiceWebhookInvalidError,
    VoiceWebhookReplayError,
)
from nexus_ai.voice.providers.base import HttpError, HttpResponse, VoiceSessionSpec, WebhookContext
from nexus_ai.voice.providers.elevenlabs import ElevenLabsVoiceAdapter

pytestmark = pytest.mark.anyio

_FMT = AudioFormat(codec=VoiceCodec.PCM_S16LE, sample_rate=16_000)
_UUID_A = uuid.UUID("01a00000-0000-7000-8000-000000000001")
_SECRET = SecretMaterial(
    CredentialType.PROVIDER_SECRET_SET, {"api_key": "sk-eleven", "webhook_secret": "wh"}
)
_SPEC = VoiceSessionSpec(
    provider="elevenlabs",
    external_account_id="acct-1",
    provider_voice_ref="agent_123",
    provider_model_ref=None,
    input_format=_FMT,
    output_format=_FMT,
    provider_api_base="https://api.elevenlabs.io",
)


class _Http:
    def __init__(self, *outcomes: Any) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.requests.append({"method": method, "url": url, "headers": dict(headers)})
        outcome = self._outcomes.pop(0) if self._outcomes else HttpResponse(200, {}, b"{}")
        if isinstance(outcome, HttpError):
            raise outcome
        return outcome


async def test_open_session_fetches_a_signed_url_and_never_puts_the_key_on_the_wire() -> None:
    adapter = ElevenLabsVoiceAdapter()
    http = _Http(
        HttpResponse(
            200, {}, json.dumps({"signed_url": "wss://api.elevenlabs.io/x?token=abc"}).encode()
        )
    )
    init = await adapter.open_session(_SPEC, _SECRET, http)
    assert init.url.startswith("wss://")
    req = http.requests[0]
    assert req["method"] == "GET"
    assert req["url"].startswith("https://api.elevenlabs.io/v1/convai/conversation/get-signed-url")
    assert req["headers"]["xi-api-key"] == "sk-eleven"
    assert "sk-eleven" not in req["url"]


async def test_open_session_maps_auth_and_error_statuses() -> None:
    adapter = ElevenLabsVoiceAdapter()
    with pytest.raises(VoiceNotAuthorizedError):
        await adapter.open_session(_SPEC, _SECRET, _Http(HttpResponse(401, {}, b"{}")))
    with pytest.raises(VoiceProviderError):
        await adapter.open_session(_SPEC, _SECRET, _Http(HttpResponse(500, {}, b"{}")))
    with pytest.raises(VoiceProviderTimeoutError):
        await adapter.open_session(_SPEC, _SECRET, _Http(HttpError("read timed out", timeout=True)))


async def test_open_session_requires_an_api_key() -> None:
    adapter = ElevenLabsVoiceAdapter()
    incomplete = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"webhook_secret": "wh"})
    with pytest.raises(VoiceProviderError):
        await adapter.open_session(_SPEC, incomplete, _Http())


def test_parse_frame_normalizes_every_documented_server_message() -> None:
    adapter = ElevenLabsVoiceAdapter()
    audio_b64 = base64.b64encode(b"\x00\x01\x02\x03").decode()

    started = adapter.parse_frame(
        json.dumps(
            {
                "type": "conversation_initiation_metadata",
                "conversation_initiation_metadata_event": {"conversation_id": "conv-9"},
            }
        )
    )
    assert started is not None and started.kind is VoiceProviderEventKind.SESSION_STARTED
    assert started.provider_session_id == "conv-9"

    audio = adapter.parse_frame(
        json.dumps({"type": "audio", "audio_event": {"audio_base_64": audio_b64, "event_id": 7}})
    )
    assert audio is not None and audio.kind is VoiceProviderEventKind.AUDIO_OUTPUT
    assert audio.audio == b"\x00\x01\x02\x03"

    transcript = adapter.parse_frame(
        json.dumps(
            {"type": "user_transcript", "user_transcription_event": {"user_transcript": "hi there"}}
        )
    )
    assert transcript is not None and transcript.kind is VoiceProviderEventKind.TRANSCRIPT

    ping = adapter.parse_frame(json.dumps({"type": "ping", "ping_event": {"event_id": 3}}))
    assert ping is not None and ping.kind is VoiceProviderEventKind.KEEPALIVE
    assert adapter.keepalive_reply(3) == json.dumps({"type": "pong", "event_id": 3})

    assert adapter.parse_frame(json.dumps({"type": "vad_score", "vad_score_event": {}})) is None

    with pytest.raises(VoiceProtocolError):
        adapter.parse_frame(json.dumps({"type": "totally_unknown"}))
    with pytest.raises(VoiceProtocolError):
        adapter.parse_frame("not json")


def test_serialize_audio_and_init_are_documented_shapes() -> None:
    adapter = ElevenLabsVoiceAdapter()
    assert (
        json.loads(adapter.serialize_audio(b"abc"))["user_audio_chunk"]
        == base64.b64encode(b"abc").decode()
    )
    init = json.loads(adapter.serialize_init(_SPEC))
    assert init["type"] == "conversation_initiation_client_data"


def test_verify_webhook_enforces_signature_and_freshness() -> None:
    adapter = ElevenLabsVoiceAdapter()
    body = json.dumps(
        {"type": "post_call_transcription", "data": {"conversation_id": "c1"}}
    ).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"wh", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ok = WebhookContext("POST", {"ElevenLabs-Signature": f"t={ts},v0={digest}"}, {}, body)
    adapter.verify_webhook(ok, _SECRET, tolerance_seconds=300)
    parsed = adapter.parse_webhook(ok)
    assert parsed.provider_event_id == "c1" and parsed.session_ended

    stale_ts = str(int(time.time()) - 5000)
    stale_digest = hmac.new(b"wh", f"{stale_ts}.".encode() + body, hashlib.sha256).hexdigest()
    stale = WebhookContext(
        "POST", {"ElevenLabs-Signature": f"t={stale_ts},v0={stale_digest}"}, {}, body
    )
    with pytest.raises(VoiceWebhookReplayError):
        adapter.verify_webhook(stale, _SECRET, tolerance_seconds=300)
    with pytest.raises(VoiceWebhookInvalidError):
        adapter.verify_webhook(WebhookContext("POST", {}, {}, body), _SECRET, tolerance_seconds=300)
    with pytest.raises(VoiceWebhookInvalidError):
        adapter.verify_webhook(
            WebhookContext("POST", {"ElevenLabs-Signature": f"t={ts},v0=deadbeef"}, {}, body),
            _SECRET,
            tolerance_seconds=300,
        )


def test_fake_provider_parses_and_serializes_the_neutral_protocol() -> None:
    import base64
    import json as _json

    from nexus_ai.voice.providers.fake import FakeVoiceProvider

    p = FakeVoiceProvider()
    audio = p.parse_frame(
        _json.dumps({"type": "audio", "chunk": base64.b64encode(b"abcd").decode()})
    )
    assert audio is not None and audio.audio == b"abcd"
    err = p.parse_frame(_json.dumps({"type": "error", "reason": "bad"}))
    assert err is not None and err.error_label == "bad"
    ended = p.parse_frame(_json.dumps({"type": "session_ended"}))
    assert ended is not None and ended.kind.name == "SESSION_ENDED"
    with pytest.raises(VoiceProtocolError):
        p.parse_frame(_json.dumps({"type": "nonsense"}))
    assert "user_audio" not in p.serialize_audio(b"x")  # fake uses its own shape
    assert _json.loads(p.keepalive_reply(9))["event_id"] == 9


# --- BLOCKER 1: WebSocket SSRF / provider URL trust -----------------------------------


@pytest.mark.parametrize(
    "signed_url",
    [
        "wss://127.0.0.1/stream",  # loopback
        "wss://169.254.169.254/latest/meta-data",  # cloud metadata
        "wss://10.0.0.5/stream",  # RFC1918
        "wss://192.168.1.1/stream",  # RFC1918
        "wss://internal.corp.local/stream",  # unauthorised hostname
        "wss://evil.example.com/stream",  # unauthorised public hostname
        "wss://user:pw@api.elevenlabs.io/stream",  # userinfo
        "wss://api.elevenlabs.io:22/stream",  # unsafe port
        "https://api.elevenlabs.io/stream",  # not wss
    ],
)
async def test_open_session_rejects_untrusted_signed_ws_url(signed_url: str) -> None:
    adapter = ElevenLabsVoiceAdapter()
    http = _Http(HttpResponse(200, {}, json.dumps({"signed_url": signed_url}).encode()))
    with pytest.raises(VoiceProviderError):
        await adapter.open_session(_SPEC, _SECRET, http)


async def test_open_session_accepts_the_official_elevenlabs_ws_host() -> None:
    adapter = ElevenLabsVoiceAdapter()
    http = _Http(
        HttpResponse(
            200,
            {},
            json.dumps(
                {"signed_url": "wss://api.elevenlabs.io/v1/convai/conversation?token=t"}
            ).encode(),
        )
    )
    init = await adapter.open_session(_SPEC, _SECRET, http)
    assert init.ws_host == "api.elevenlabs.io" and init.ws_port == 443


async def test_open_session_uses_only_the_configured_api_base_never_a_request_option() -> None:
    from nexus_ai.voice.providers.base import VoiceSessionSpec

    adapter = ElevenLabsVoiceAdapter()
    # a spec with NO provider_api_base (as if a caller tried to omit / override it)
    naked = VoiceSessionSpec(
        provider="elevenlabs",
        external_account_id="acct-1",
        provider_voice_ref="agent_1",
        provider_model_ref=None,
        input_format=_FMT,
        output_format=_FMT,
        options={"language": "en"},
    )
    with pytest.raises(Exception):  # noqa: B017 - VoiceConfigInvalidError
        await adapter.open_session(naked, _SECRET, _Http())
    # a caller CANNOT smuggle an endpoint through options (allow-list on the request model)
    from pydantic import ValidationError

    from nexus_ai.voice.entities import StartVoiceSessionRequest

    with pytest.raises(ValidationError):
        StartVoiceSessionRequest(
            call_id=_UUID_A,
            media_session_id=_UUID_A,
            provider_account_id=_UUID_A,
            voice_profile_id=_UUID_A,
            options={"api_base": "https://evil.example"},
        )


# --- protocol reconciliation ---------------------------------------------------------


def test_agent_response_correction_is_normalized_as_a_transport_fact() -> None:
    adapter = ElevenLabsVoiceAdapter()
    ev = adapter.parse_frame(
        json.dumps(
            {
                "type": "agent_response_correction",
                "agent_response_correction_event": {"corrected_agent_response": "actually, no"},
            }
        )
    )
    assert ev is not None and ev.kind is VoiceProviderEventKind.AGENT_TEXT
    assert ev.text == "actually, no"


def test_client_tool_call_is_ignored_never_executed_in_p12() -> None:
    adapter = ElevenLabsVoiceAdapter()
    for frame_type in ("client_tool_call", "client_tool_result", "mcp_tool_call"):
        assert adapter.parse_frame(json.dumps({"type": frame_type, "x": 1})) is None, frame_type
    # a genuinely unknown frame still fails closed
    with pytest.raises(VoiceProtocolError):
        adapter.parse_frame(json.dumps({"type": "totally_made_up_frame"}))
