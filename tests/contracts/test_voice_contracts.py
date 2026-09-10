"""Voice stable contracts — session states, provider-neutral adapter, request/response
schemas, error taxonomy, P04 event schemas, RBAC scopes, OpenAPI governed surface, and
the hard boundary that NXS-P13 stays PLANNED and P12 carries no autonomous reasoning."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.domain.auth.rbac import PERMISSION_IDS, PermissionKey
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.voice.audio import VoiceCodec
from nexus_ai.voice.entities import (
    CreateVoiceProfileRequest,
    StartVoiceSessionRequest,
    VoiceProvider,
    VoiceProviderEventKind,
    VoiceSessionDirection,
    VoiceSessionView,
)
from nexus_ai.voice.errors import VOICE_ERRORS
from nexus_ai.voice.providers.base import VoiceProviderAdapter
from nexus_ai.voice.providers.registry import known_voice_providers
from nexus_ai.voice.state_machine import VoiceSessionState

_ROOT = Path(__file__).resolve().parents[2]


def test_session_states_directions_codecs_and_providers_are_frozen() -> None:
    assert [s.value for s in VoiceSessionState] == [
        "PENDING",
        "CONNECTING",
        "CONNECTED",
        "STREAMING",
        "ENDING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]
    assert [d.value for d in VoiceSessionDirection] == ["INBOUND", "OUTBOUND"]
    assert {c.value for c in VoiceCodec} == {"pcm_s16le", "mulaw", "alaw"}
    assert [p.value for p in VoiceProvider] == ["elevenlabs", "fake"]
    assert known_voice_providers() == ("elevenlabs", "fake")
    assert {k.value for k in VoiceProviderEventKind} == {
        "SESSION_STARTED",
        "SESSION_ENDED",
        "AUDIO_OUTPUT",
        "TRANSCRIPT",
        "AGENT_TEXT",
        "INTERRUPTION",
        "KEEPALIVE",
        "ERROR",
    }


def test_handoff_state_machine_is_truthful() -> None:
    from nexus_ai.voice.entities import VoiceHandoffState

    # a three-state lifecycle: request only moves to PENDING_HUMAN; HUMAN needs an
    # authoritative confirmation (a future NXS-P17 responsibility).
    assert [s.value for s in VoiceHandoffState] == ["AI", "PENDING_HUMAN", "HUMAN"]


def test_start_session_options_are_a_fixed_allow_list_never_an_endpoint() -> None:
    from nexus_ai.voice.entities import ALLOWED_SESSION_OPTION_KEYS

    for banned in ("api_base", "url", "endpoint", "host", "ws_url", "signed_url", "api_key"):
        assert banned not in ALLOWED_SESSION_OPTION_KEYS
    # the ElevenLabs adapter never reads an endpoint from options
    source = (_ROOT / "src" / "nexus_ai" / "voice" / "providers" / "elevenlabs.py").read_text()
    assert 'options.get("api_base"' not in source
    assert "options[" not in source or "api_base" not in source


def test_provider_adapter_contract_is_provider_neutral() -> None:
    methods = {name for name, _ in inspect.getmembers(VoiceProviderAdapter, inspect.isfunction)}
    assert {
        "open_session",
        "serialize_init",
        "serialize_audio",
        "keepalive_reply",
        "parse_frame",
        "verify_webhook",
        "parse_webhook",
    } <= methods
    # no ElevenLabs identifier anywhere in the neutral contract
    source = inspect.getsource(VoiceProviderAdapter)
    assert "eleven" not in source.lower()


def test_start_session_request_has_no_provider_or_transport_surface() -> None:
    fields = set(StartVoiceSessionRequest.model_fields)
    assert fields == {
        "call_id",
        "media_session_id",
        "provider_account_id",
        "voice_profile_id",
        "correlation_id",
        "idempotency_key",
        "options",
    }
    for banned in ("agent_id", "voice_id", "api_key", "ws_url", "signed_url", "raw", "config"):
        assert banned not in fields
    assert StartVoiceSessionRequest.model_config.get("extra") == "forbid"
    assert CreateVoiceProfileRequest.model_config.get("extra") == "forbid"


def test_session_view_carries_no_credential_or_raw_audio() -> None:
    fields = set(VoiceSessionView.model_fields)
    for banned in (
        "api_key",
        "signed_url",
        "ws_url",
        "audio",
        "transcript",
        "authorization",
        "credential",
        "sdp",
    ):
        assert banned not in fields


def test_error_taxonomy_is_stable_unique_and_rfc9457() -> None:
    codes = [e.code for e in VOICE_ERRORS]
    assert len(codes) == len(set(codes))
    assert set(codes) == {
        "NXS_VOICE_PROVIDER_NOT_FOUND",
        "NXS_VOICE_ACCOUNT_NOT_FOUND",
        "NXS_VOICE_PROFILE_NOT_FOUND",
        "NXS_VOICE_SESSION_NOT_FOUND",
        "NXS_VOICE_MEDIA_NOT_READY",
        "NXS_VOICE_CONFIG_INVALID",
        "NXS_VOICE_UNSUPPORTED_AUDIO",
        "NXS_VOICE_INVALID_STATE",
        "NXS_VOICE_PROVIDER_ERROR",
        "NXS_VOICE_PROVIDER_TIMEOUT",
        "NXS_VOICE_CONNECTION_FAILED",
        "NXS_VOICE_PROTOCOL_ERROR",
        "NXS_VOICE_WEBHOOK_INVALID",
        "NXS_VOICE_WEBHOOK_REPLAY",
        "NXS_VOICE_IDEMPOTENCY_CONFLICT",
        "NXS_VOICE_NOT_AUTHORIZED",
    }
    for error in VOICE_ERRORS:
        assert issubclass(error, NxsError)
        assert 400 <= error.status < 600


def test_p04_voice_event_payloads_are_registered_and_strict() -> None:
    import nexus_ai.voice.events  # noqa: F401 - registration

    for name in (
        "voice.session.created",
        "voice.session.connecting",
        "voice.session.connected",
        "voice.session.streaming",
        "voice.session.completed",
        "voice.session.failed",
        "voice.provider.connected",
        "voice.provider.disconnected",
        "voice.transcript.partial",
        "voice.transcript.final",
        "voice.interruption.started",
        "voice.interruption.completed",
        "voice.handoff.requested",
        "voice.handoff.completed",
        "voice.usage.recorded",
    ):
        model = EVENT_REGISTRY.model_for(name, 1)
        assert model.model_config.get("extra") == "forbid"
    # transcript events carry a char count, NEVER the text body
    partial = EVENT_REGISTRY.model_for("voice.transcript.partial", 1)
    assert "char_count" in partial.model_fields and "text" not in partial.model_fields


def test_rbac_scopes_exist() -> None:
    for key in (
        PermissionKey.VOICE_READ,
        PermissionKey.VOICE_USE,
        PermissionKey.VOICE_CONFIGURE,
    ):
        assert key in PERMISSION_IDS


@pytest.mark.anyio
async def test_openapi_voice_surface_is_governed_only(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    voice_paths = [p for p in schema["paths"] if "/voice" in p]
    assert "/api/v1/voice/sessions" in voice_paths
    blob = json.dumps(schema)
    for banned in ("signed_url", "xi-api-key", "user_audio_chunk", "wss://", "ari/channels"):
        assert banned not in blob


def test_p13_stays_planned_and_p12_has_no_reasoning() -> None:
    registry = json.loads((_ROOT / ".nxs" / "phase-registry.json").read_text())
    phases = {p["id"]: p for p in registry["phases"]}
    assert phases["NXS-P13"]["status"] == "PLANNED"
    assert phases["NXS-P13"]["branch"] == "feat/nxs-p13-agent-runtime"

    # the voice package never imports the tool engine, an agent runtime or a workflow
    # engine — P12 is transport, not reasoning.
    for path in (_ROOT / "src" / "nexus_ai" / "voice").rglob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                for banned in (
                    "nexus_ai.tools",
                    "agent_runtime",
                    "nexus_ai.workflows",
                    "nexus_ai.campaigns",
                ):
                    assert banned not in stripped, (path.name, stripped)
