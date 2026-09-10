"""Voice primitives: the session state machine, audio-format governance, the request
fingerprint, the media-bridge SSRF guard, strict request models and redaction (NXS-P12)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.voice.audio import AudioFormat, VoiceCodec, normalize_audio_format
from nexus_ai.voice.bridge import plan_media_bridge
from nexus_ai.voice.entities import StartVoiceSessionRequest
from nexus_ai.voice.errors import VoiceConfigInvalidError, VoiceUnsupportedAudioError
from nexus_ai.voice.idempotency import voice_session_fingerprint
from nexus_ai.voice.redaction import redact_mapping, redact_ws_url
from nexus_ai.voice.state_machine import (
    FoldOutcome,
    VoiceSessionState,
    fold_session_state,
    is_terminal,
    session_rank,
)

_A = uuid.UUID("01a00000-0000-7000-8000-0000000000a1")
_B = uuid.UUID("01a00000-0000-7000-8000-0000000000b2")
_C = uuid.UUID("01a00000-0000-7000-8000-0000000000c3")
_GATEWAY = {"media_gateway_host": "10.20.30.40", "media_gateway_port": 40000}
_FMT = AudioFormat(codec=VoiceCodec.PCM_S16LE, sample_rate=16_000)


def _fold(current: VoiceSessionState, proposed: VoiceSessionState, **kw: object) -> object:
    return fold_session_state(
        current=current,
        current_rank=session_rank(current),
        current_provider_ts=kw.get("current_ts"),  # type: ignore[arg-type]
        current_sequence=kw.get("current_seq"),  # type: ignore[arg-type]
        proposed=proposed,
        proposed_provider_ts=kw.get("proposed_ts"),  # type: ignore[arg-type]
        proposed_sequence=kw.get("proposed_seq"),  # type: ignore[arg-type]
    )


def test_forward_transitions_apply_and_infer_intermediates() -> None:
    assert _fold(VoiceSessionState.PENDING, VoiceSessionState.CONNECTING).outcome is (
        FoldOutcome.APPLIED
    )
    # CONNECTED arriving before a delayed CONNECTING is still a legal forward move
    assert _fold(VoiceSessionState.PENDING, VoiceSessionState.STREAMING).outcome is (
        FoldOutcome.APPLIED
    )
    r = _fold(VoiceSessionState.STREAMING, VoiceSessionState.COMPLETED)
    assert r.outcome is FoldOutcome.APPLIED and r.disposition is not None


def test_stale_and_duplicate_events_are_ignored() -> None:
    assert _fold(VoiceSessionState.CONNECTED, VoiceSessionState.CONNECTING).outcome is (
        FoldOutcome.IGNORED
    )
    assert _fold(VoiceSessionState.STREAMING, VoiceSessionState.STREAMING).outcome is (
        FoldOutcome.IGNORED
    )


def test_terminal_states_are_absorbing() -> None:
    for terminal in (
        VoiceSessionState.COMPLETED,
        VoiceSessionState.FAILED,
        VoiceSessionState.CANCELLED,
    ):
        assert is_terminal(terminal)
        assert _fold(terminal, VoiceSessionState.STREAMING).outcome is FoldOutcome.IGNORED
        # a different terminal never overwrites the first
        assert _fold(terminal, VoiceSessionState.COMPLETED).state is terminal


def test_provider_sequence_precedence_is_enforced() -> None:
    stale = _fold(
        VoiceSessionState.CONNECTED, VoiceSessionState.STREAMING, current_seq=5, proposed_seq=3
    )
    assert stale.outcome is FoldOutcome.IGNORED and stale.reason == "stale-provider-order"
    fresh = _fold(
        VoiceSessionState.CONNECTED, VoiceSessionState.STREAMING, current_seq=5, proposed_seq=9
    )
    assert fresh.outcome is FoldOutcome.APPLIED
    early, late = dt.datetime(2026, 1, 1, tzinfo=dt.UTC), dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    ts_stale = _fold(
        VoiceSessionState.CONNECTED,
        VoiceSessionState.STREAMING,
        current_ts=late,
        proposed_ts=early,
    )
    assert ts_stale.outcome is FoldOutcome.IGNORED
    # a terminal is exempt: it always wins regardless of provider order
    terminal = _fold(
        VoiceSessionState.CONNECTED, VoiceSessionState.FAILED, current_seq=5, proposed_seq=1
    )
    assert terminal.outcome is FoldOutcome.APPLIED


@pytest.mark.parametrize(
    ("codec", "rate", "expected"),
    [
        ("pcm_16000", 16_000, VoiceCodec.PCM_S16LE),
        ("ulaw_8000", 8_000, VoiceCodec.MULAW),
        ("g711_alaw", 8_000, VoiceCodec.ALAW),
    ],
)
def test_normalize_audio_format_maps_provider_labels(
    codec: str, rate: int, expected: VoiceCodec
) -> None:
    fmt = normalize_audio_format(codec=codec, sample_rate=rate)
    assert fmt.codec is expected and fmt.sample_rate == rate


@pytest.mark.parametrize(
    ("codec", "rate", "channels", "frame"),
    [
        ("opus", 16_000, 1, 20),  # unknown codec
        ("pcm_16000", 11_025, 1, 20),  # unsupported rate
        ("pcm_16000", 16_000, 2, 20),  # stereo not allowed
        ("pcm_16000", 16_000, 1, 15),  # unsupported frame size
    ],
)
def test_normalize_audio_format_fails_closed(
    codec: str, rate: int, channels: int, frame: int
) -> None:
    with pytest.raises(VoiceUnsupportedAudioError):
        normalize_audio_format(codec=codec, sample_rate=rate, channels=channels, frame_ms=frame)


def test_request_fingerprint_binds_every_semantic_field() -> None:
    base = {
        "media_session_id": _A,
        "provider_account_id": _B,
        "voice_profile_id": _C,
        "options": {"language": "en", "greeting": "x"},
    }
    fp = voice_session_fingerprint(**base)  # type: ignore[arg-type]
    assert fp == voice_session_fingerprint(
        media_session_id=_A,
        provider_account_id=_B,
        voice_profile_id=_C,
        options={"greeting": "x", "language": "en"},
    )
    assert fp != voice_session_fingerprint(**{**base, "media_session_id": _B})  # type: ignore[arg-type]
    assert fp != voice_session_fingerprint(**{**base, "provider_account_id": _A})  # type: ignore[arg-type]
    assert fp != voice_session_fingerprint(**{**base, "voice_profile_id": _A})  # type: ignore[arg-type]
    assert fp != voice_session_fingerprint(  # type: ignore[arg-type]
        **{**base, "options": {"language": "fr", "greeting": "x"}}
    )


@pytest.mark.parametrize(
    "config",
    [
        {"media_gateway_host": "127.0.0.1", "media_gateway_port": 40000},  # loopback
        {"media_gateway_host": "169.254.169.254", "media_gateway_port": 80},  # cloud metadata
        {"media_gateway_host": "8.8.8.8", "media_gateway_port": 40000},  # public
        {"media_gateway_host": "10.0.0.1", "media_gateway_port": 22},  # port too low
        {"media_gateway_host": "10.0.0.1", "media_gateway_port": "not-a-port"},  # bad port
        {},  # missing gateway
    ],
)
def test_media_bridge_rejects_unsafe_or_missing_gateways(config: dict[str, object]) -> None:
    with pytest.raises(VoiceConfigInvalidError):
        plan_media_bridge(bridge_id="bridge-1", configuration=config, audio_format=_FMT)


@pytest.mark.parametrize("bridge_id", ["", "bridge 1", "bridge;rm -rf", "b\r\nBad"])
def test_media_bridge_rejects_injected_bridge_ids(bridge_id: str) -> None:
    with pytest.raises(VoiceConfigInvalidError):
        plan_media_bridge(bridge_id=bridge_id, configuration=_GATEWAY, audio_format=_FMT)


def test_media_bridge_plan_is_internally_generated() -> None:
    plan = plan_media_bridge(
        bridge_id="asterisk-bridge-42", configuration=_GATEWAY, audio_format=_FMT
    )
    assert plan.gateway_host == "10.20.30.40" and plan.gateway_port == 40000
    assert plan.stream_ref.startswith("vs-") and plan.audio_format is _FMT


def test_start_session_request_has_no_transport_surface() -> None:
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
    for banned in ("agent_id", "voice_id", "api_key", "ws_url", "url", "signed_url", "config"):
        assert banned not in fields


def test_start_session_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        StartVoiceSessionRequest(
            call_id=_A,
            media_session_id=_B,
            provider_account_id=_C,
            voice_profile_id=_A,
            api_key="sk-secret",  # type: ignore[call-arg]
        )


def test_redaction_hides_secrets_and_query_strings() -> None:
    redacted = redact_mapping({"xi-api-key": "sk-abc", "Authorization": "Bearer z", "state": "ok"})
    assert redacted["xi-api-key"] == "<redacted>"
    assert redacted["Authorization"] == "<redacted>"
    assert redacted["state"] == "ok"
    assert redact_ws_url("wss://api.elevenlabs.io/v1/convai?token=SECRET&x=1") == (
        "wss://api.elevenlabs.io/v1/convai"
    )
