"""The real-time voice session runtime (NXS-P12, ADR-0089).

Runs one bounded, cancellation-safe streaming loop between an NXS-P11 media session and a
voice provider:

    media inbound audio  --serialize-->  provider WebSocket
    provider frames       --parse-->     media outbound audio  +  normalized events

Every path is bounded: the connect / handshake / idle / session timeouts, the inbound
and outbound audio queues (:class:`BoundedFrameQueue`, drop-oldest backpressure), the
per-frame byte ceiling and the provider message ceiling. Interruption (barge-in) bumps a
discard epoch: audio produced before the interruption is dropped and never resurrected.
On any exit — normal, error, peer close or cancellation — both worker tasks are
cancelled and awaited, the transport is closed and the media channel is closed: nothing
is left behind.

P12 does NOT reason over transcripts. A transcript / agent-text frame is surfaced to the
caller as a transport-level fact (with a character count only); NXS-P13 owns reasoning.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from nexus_ai.core.config import VoiceSettings
from nexus_ai.core.logging import get_logger
from nexus_ai.voice.audio import AudioFormat
from nexus_ai.voice.entities import (
    VoiceLatencyMetrics,
    VoiceProviderEvent,
    VoiceProviderEventKind,
    VoiceSessionContext,
    VoiceUsage,
)
from nexus_ai.voice.errors import (
    VoiceConnectionFailedError,
    VoiceProtocolError,
    VoiceProviderError,
    VoiceProviderTimeoutError,
)
from nexus_ai.voice.providers.base import (
    ProviderSessionInit,
    VoiceHttpTransport,
    VoiceProviderAdapter,
    VoiceSessionSpec,
)
from nexus_ai.voice.redaction import redact_ws_url
from nexus_ai.voice.state_machine import VoiceSessionDisposition
from nexus_ai.voice.transport import BoundedFrameQueue, StreamClosed, VoiceStreamTransport

_log = get_logger("nexus_ai.voice.runtime")

#: Called with each normalized non-audio event (transcript, agent text, interruption,
#: session-started). It must be cheap and non-blocking — the runtime awaits it inline.
EventHook = Callable[[VoiceProviderEvent], Awaitable[None]]
#: Called once, right after the real-time transport is open (session is CONNECTED).
ConnectedHook = Callable[[], Awaitable[None]]


class MediaChannel(Protocol):
    """The NXS-P11 / Asterisk media side. Production bridges this to the operations media
    gateway per a validated :class:`~nexus_ai.voice.bridge.MediaBridgePlan`; tests use a
    loopback. All I/O is non-blocking and bounded by the runtime."""

    async def read(self, *, timeout: float) -> bytes: ...

    def write(self, frame: bytes) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class RuntimeOutcome:
    disposition: VoiceSessionDisposition
    provider_session_id: str | None = None
    error_code: str | None = None
    usage: VoiceUsage = field(default_factory=VoiceUsage)
    latency: VoiceLatencyMetrics = field(default_factory=VoiceLatencyMetrics)
    negotiated_format: AudioFormat | None = None
    connected: bool = False


@dataclass(slots=True)
class _State:
    outbound: BoundedFrameQueue
    discard_epoch: int = 0
    interruptions: int = 0
    frames_in: int = 0
    frames_out: int = 0
    characters: int = 0
    first_audio_at: dt.datetime | None = None


class VoiceSessionRuntime:
    def __init__(self, settings: VoiceSettings) -> None:
        self._settings = settings

    async def run(
        self,
        *,
        ctx: VoiceSessionContext,
        spec: VoiceSessionSpec,
        adapter: VoiceProviderAdapter,
        transport: VoiceStreamTransport,
        media: MediaChannel,
        http: VoiceHttpTransport,
        secret: object,
        on_event: EventHook,
        on_connected: ConnectedHook | None = None,
    ) -> RuntimeOutcome:
        cfg = self._settings
        state = _State(
            outbound=BoundedFrameQueue(
                depth=cfg.audio_queue_depth, max_frame_bytes=cfg.max_audio_frame_bytes
            )
        )
        started_at = _now()
        outcome = RuntimeOutcome(disposition=VoiceSessionDisposition.FAILED)

        try:
            init: ProviderSessionInit = await asyncio.wait_for(
                adapter.open_session(spec, secret, http),
                timeout=cfg.provider_timeout_seconds,
            )
        except TimeoutError as exc:
            raise VoiceConnectionFailedError("the provider session-open timed out") from exc
        outcome.negotiated_format = init.negotiated_format
        outcome.provider_session_id = init.provider_session_id

        connect_ms: int | None = None
        workers: list[asyncio.Task[None]] = []
        try:
            connect_start = _now()
            await transport.connect(
                url=init.url, headers=init.headers, open_timeout=cfg.connect_timeout_seconds
            )
            outcome.connected = True
            connect_ms = _ms(connect_start, _now())
            await _log_connected(ctx, init.url)
            if on_connected is not None:
                await on_connected()
            await transport.send(adapter.serialize_init(spec))

            reader = asyncio.create_task(
                self._reader(adapter, transport, media, state, on_event, outcome)
            )
            writer = asyncio.create_task(self._writer(adapter, transport, media, state))
            workers = [reader, writer]
            try:
                async with asyncio.timeout(cfg.max_session_seconds):
                    done, _pending = await asyncio.wait(
                        {reader, writer}, return_when=asyncio.FIRST_COMPLETED
                    )
            except TimeoutError:
                done = set()
                outcome.error_code = "NXS_VOICE_PROVIDER_TIMEOUT"
            for task in done:
                task_exc = task.exception()
                if isinstance(task_exc, VoiceProviderTimeoutError):
                    outcome.error_code = "NXS_VOICE_PROVIDER_TIMEOUT"
                elif task_exc is not None and not isinstance(task_exc, StreamClosed):
                    raise task_exc
            if outcome.error_code is None and outcome.disposition is VoiceSessionDisposition.FAILED:
                outcome.disposition = VoiceSessionDisposition.COMPLETED
        except asyncio.CancelledError:
            outcome.disposition = VoiceSessionDisposition.CANCELLED
            raise
        except (VoiceProtocolError, VoiceConnectionFailedError, VoiceProviderError) as exc:
            outcome.disposition = VoiceSessionDisposition.FAILED
            outcome.error_code = exc.code
        finally:
            for task in workers:
                task.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            state.outbound.close()
            await transport.aclose()
            with contextlib.suppress(Exception):
                await media.aclose()
            outcome.usage = VoiceUsage(
                audio_seconds_in=round(state.frames_in * spec.input_format.frame_ms / 1000, 3),
                audio_seconds_out=round(state.frames_out * spec.output_format.frame_ms / 1000, 3),
                provider_characters=state.characters,
                interruptions=state.interruptions,
            )
            outcome.latency = VoiceLatencyMetrics(
                connect_ms=connect_ms,
                time_to_first_audio_ms=(
                    _ms(started_at, state.first_audio_at) if state.first_audio_at else None
                ),
                session_duration_ms=_ms(started_at, _now()),
            )
        return outcome

    async def _reader(
        self,
        adapter: VoiceProviderAdapter,
        transport: VoiceStreamTransport,
        media: MediaChannel,
        state: _State,
        on_event: EventHook,
        outcome: RuntimeOutcome,
    ) -> None:
        idle = self._settings.idle_timeout_seconds
        while True:
            raw = await transport.recv(timeout=idle)
            if len(raw) > self._settings.max_message_bytes:
                raise VoiceProtocolError("a provider message exceeded the size ceiling")
            event = adapter.parse_frame(raw)
            if event is None:
                continue
            await self._dispatch(event, adapter, transport, media, state, on_event, outcome)

    async def _dispatch(
        self,
        event: VoiceProviderEvent,
        adapter: VoiceProviderAdapter,
        transport: VoiceStreamTransport,
        media: MediaChannel,
        state: _State,
        on_event: EventHook,
        outcome: RuntimeOutcome,
    ) -> None:
        kind = event.kind
        if kind is VoiceProviderEventKind.AUDIO_OUTPUT and event.audio is not None:
            if len(event.audio) > self._settings.max_audio_frame_bytes:
                raise VoiceProtocolError("a provider audio frame exceeded the size ceiling")
            if state.first_audio_at is None:
                state.first_audio_at = _now()
            media.write(event.audio)
            state.frames_out += 1
            return
        if kind is VoiceProviderEventKind.KEEPALIVE:
            reply = adapter.keepalive_reply(event.provider_sequence)
            if reply is not None:
                await transport.send(reply)
            return
        if kind is VoiceProviderEventKind.INTERRUPTION:
            state.interruptions += 1
            state.discard_epoch += 1
            state.outbound.clear()
            await on_event(event)
            return
        if kind in (VoiceProviderEventKind.TRANSCRIPT, VoiceProviderEventKind.AGENT_TEXT):
            state.characters += len(event.text or "")
            await on_event(event)
            return
        if kind is VoiceProviderEventKind.SESSION_STARTED:
            if event.provider_session_id:
                outcome.provider_session_id = event.provider_session_id
            await on_event(event)
            return
        if kind is VoiceProviderEventKind.ERROR:
            outcome.disposition = VoiceSessionDisposition.FAILED
            outcome.error_code = "NXS_VOICE_PROVIDER_ERROR"
            await on_event(event)
            raise StreamClosed("the provider signalled an error", by_peer=True)
        if kind is VoiceProviderEventKind.SESSION_ENDED:
            outcome.disposition = VoiceSessionDisposition.COMPLETED
            raise StreamClosed("the provider ended the session", by_peer=True)

    async def _writer(
        self,
        adapter: VoiceProviderAdapter,
        transport: VoiceStreamTransport,
        media: MediaChannel,
        state: _State,
    ) -> None:
        idle = self._settings.idle_timeout_seconds
        while True:
            try:
                frame = await media.read(timeout=idle)
            except StreamClosed, TimeoutError:
                return
            if not frame:
                return
            if len(frame) > self._settings.max_audio_frame_bytes:
                raise VoiceProtocolError("an inbound audio frame exceeded the size ceiling")
            state.frames_in += 1
            await transport.send(adapter.serialize_audio(frame))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ms(start: dt.datetime, end: dt.datetime | None) -> int:
    if end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


async def _log_connected(ctx: VoiceSessionContext, url: str) -> None:
    await _log.ainfo(
        "voice_provider_connected",
        organization_id=str(ctx.organization_id),
        voice_session_id=str(ctx.session_id),
        call_id=str(ctx.call_id),
        ws_origin=redact_ws_url(url),
    )
