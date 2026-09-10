"""Audio format governance (NXS-P12).

Audio contracts are explicit and fail closed. A provider-supplied codec name is NEVER
executable configuration: it is validated against a fixed Nexus allow-list and mapped to
a frozen :class:`AudioFormat`. No silent lossy transformation happens here — P12 performs
no transcoding; it only asserts that the negotiated format is one both sides agreed on.
If a future phase needs resampling it must be an explicit, tested, bounded primitive.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from nexus_ai.voice.errors import VoiceUnsupportedAudioError


class VoiceCodec(StrEnum):
    """The only codecs P12 will carry. Telephony-grade PCM and G.711 companded audio."""

    PCM_S16LE = "pcm_s16le"
    MULAW = "mulaw"
    ALAW = "alaw"


class VoiceMediaDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    BIDIRECTIONAL = "BIDIRECTIONAL"


#: Sample rates P12 accepts. 8k is telephony; the rest are common real-time voice rates.
_ALLOWED_SAMPLE_RATES: frozenset[int] = frozenset({8_000, 16_000, 22_050, 24_000, 48_000})
_ALLOWED_CHANNELS: frozenset[int] = frozenset({1})
_ALLOWED_FRAME_MS: frozenset[int] = frozenset({10, 20, 30, 40, 60})


class AudioFormat(BaseModel):
    """A frozen, validated audio contract. Both the media side (NXS-P11 / Asterisk) and
    the provider must agree on exactly this."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    codec: VoiceCodec
    sample_rate: Annotated[int, Field(ge=8_000, le=48_000)]
    channels: Annotated[int, Field(ge=1, le=1)] = 1
    frame_ms: Annotated[int, Field(ge=10, le=60)] = 20

    def bytes_per_frame(self) -> int:
        samples = int(self.sample_rate * self.frame_ms / 1000)
        width = 2 if self.codec is VoiceCodec.PCM_S16LE else 1
        return samples * width * self.channels


#: Provider codec label -> Nexus codec. The ONLY place a provider string becomes config.
_PROVIDER_CODEC_ALIASES: dict[str, VoiceCodec] = {
    "pcm_s16le": VoiceCodec.PCM_S16LE,
    "pcm": VoiceCodec.PCM_S16LE,
    "pcm_16000": VoiceCodec.PCM_S16LE,
    "pcm_8000": VoiceCodec.PCM_S16LE,
    "pcm_16": VoiceCodec.PCM_S16LE,
    "ulaw": VoiceCodec.MULAW,
    "mulaw": VoiceCodec.MULAW,
    "ulaw_8000": VoiceCodec.MULAW,
    "g711_ulaw": VoiceCodec.MULAW,
    "alaw": VoiceCodec.ALAW,
    "alaw_8000": VoiceCodec.ALAW,
    "g711_alaw": VoiceCodec.ALAW,
}


def normalize_audio_format(
    *,
    codec: str,
    sample_rate: int,
    channels: int = 1,
    frame_ms: int = 20,
) -> AudioFormat:
    """Validate raw (possibly provider-supplied) audio parameters into a frozen
    :class:`AudioFormat`, or fail closed with ``NXS_VOICE_UNSUPPORTED_AUDIO``."""
    mapped = _PROVIDER_CODEC_ALIASES.get(str(codec).strip().lower())
    if mapped is None:
        raise VoiceUnsupportedAudioError(f"audio codec {codec!r} is not supported")
    if sample_rate not in _ALLOWED_SAMPLE_RATES:
        raise VoiceUnsupportedAudioError(f"audio sample rate {sample_rate!r} is not supported")
    if channels not in _ALLOWED_CHANNELS:
        raise VoiceUnsupportedAudioError("only single-channel (mono) audio is supported")
    if frame_ms not in _ALLOWED_FRAME_MS:
        raise VoiceUnsupportedAudioError(f"audio frame size {frame_ms!r}ms is not supported")
    return AudioFormat(codec=mapped, sample_rate=sample_rate, channels=channels, frame_ms=frame_ms)


def formats_compatible(a: AudioFormat, b: AudioFormat) -> bool:
    """True when two formats can be carried on the same stream without transcoding."""
    return a.codec == b.codec and a.sample_rate == b.sample_rate and a.channels == b.channels
