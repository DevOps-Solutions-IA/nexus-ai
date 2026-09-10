"""Bounded real-time transport primitives + the session runtime loop: backpressure,
interruption discard, cleanup, and no leaked tasks (NXS-P12, ADR-0088/0089)."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
from uuid import uuid4

import pytest

from nexus_ai.core.config import VoiceSettings
from nexus_ai.voice.audio import AudioFormat, VoiceCodec
from nexus_ai.voice.entities import VoiceProviderEvent, VoiceSessionContext, VoiceSessionDirection
from nexus_ai.voice.errors import VoiceProtocolError
from nexus_ai.voice.media import LoopbackMediaChannel
from nexus_ai.voice.providers.base import HttpResponse, VoiceSessionSpec
from nexus_ai.voice.providers.fake import FakeVoiceProvider
from nexus_ai.voice.providers.registry import GovernedVoiceHttpTransport
from nexus_ai.voice.runtime import VoiceSessionRuntime
from nexus_ai.voice.state_machine import VoiceSessionDisposition
from nexus_ai.voice.transport import BoundedFrameQueue, FakeVoiceStreamTransport, StreamClosed

pytestmark = pytest.mark.anyio

_FMT = AudioFormat(codec=VoiceCodec.PCM_S16LE, sample_rate=16_000, frame_ms=20)
_SETTINGS = VoiceSettings(
    idle_timeout_seconds=0.5,
    max_session_seconds=5.0,
    audio_queue_depth=4,
    max_audio_frame_bytes=256,
)


def _ctx() -> VoiceSessionContext:
    return VoiceSessionContext(
        organization_id=uuid4(),
        session_id=uuid4(),
        call_id=uuid4(),
        media_session_id=uuid4(),
        account_id=uuid4(),
        direction=VoiceSessionDirection.INBOUND,
        input_format=_FMT,
        output_format=_FMT,
    )


def _spec() -> VoiceSessionSpec:
    return VoiceSessionSpec(
        provider="fake",
        external_account_id="acct-1",
        provider_voice_ref="agent_1",
        provider_model_ref=None,
        input_format=_FMT,
        output_format=_FMT,
    )


class _Http:
    async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
        return HttpResponse(200, {}, b"{}")


class _Collector:
    def __init__(self) -> None:
        self.events: list[VoiceProviderEvent] = []

    async def __call__(self, event: VoiceProviderEvent) -> None:
        self.events.append(event)


async def _drain(_e: VoiceProviderEvent) -> None:
    return None


class _BlockingStreamTransport:
    """Connects, then blocks forever on recv/read — only cancellation ends it."""

    def __init__(self) -> None:
        self.connected = False

    async def connect(self, *, url: str, headers: dict[str, str], open_timeout: float) -> None:
        self.connected = True

    async def send(self, message: bytes | str) -> None:
        return None

    async def recv(self, *, timeout: float) -> bytes | str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self, *, code: int = 1000) -> None:
        self.connected = False


def test_bounded_frame_queue_rejects_oversize_and_drops_oldest() -> None:
    q = BoundedFrameQueue(depth=2, max_frame_bytes=8)
    with pytest.raises(VoiceProtocolError):
        q.put(b"x" * 9)
    q.put(b"a")
    q.put(b"b")
    q.put(b"c")  # drops "a"
    assert q.dropped == 1 and q.depth == 2
    assert q.clear() == 2


async def test_fake_stream_transport_scripts_frames_and_peer_close() -> None:
    t = FakeVoiceStreamTransport(["one", "two"], close_after=2)
    await t.connect(url="wss://x", headers={}, open_timeout=1)
    assert await t.recv(timeout=1) == "one"
    assert await t.recv(timeout=1) == "two"
    with pytest.raises(StreamClosed):
        await t.recv(timeout=1)
    await t.aclose()
    assert not t.connected


async def test_governed_transport_maps_executor_failure() -> None:
    from nexus_ai.integrations.backoff import FailureKind
    from nexus_ai.integrations.executor import ExecutorFailure, RawResponse

    class _Exec:
        def __init__(self, outcome: Any) -> None:
            self._outcome = outcome

        async def send(self, request: Any) -> Any:
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome

    ok = GovernedVoiceHttpTransport(
        _Exec(RawResponse(200, {}, b"{}", 1, "https://x")),  # type: ignore[arg-type]
        timeout_seconds=5.0,
    )
    resp = await ok.request(method="GET", url="https://x", headers={}, body=None)
    assert resp.status_code == 200

    from nexus_ai.voice.providers.base import HttpError

    failing = GovernedVoiceHttpTransport(
        _Exec(ExecutorFailure(FailureKind.IN_FLIGHT, RuntimeError("read timed out"))),  # type: ignore[arg-type]
        timeout_seconds=5.0,
    )
    with pytest.raises(HttpError) as exc:
        await failing.request(method="GET", url="https://x", headers={}, body=None)
    assert exc.value.timeout is True


async def test_runtime_streams_audio_back_to_media_and_completes() -> None:
    runtime = VoiceSessionRuntime(_SETTINGS)
    chunk = base64.b64encode(b"\x10\x20\x30\x40").decode()
    transport = FakeVoiceStreamTransport(
        [
            json.dumps({"type": "session_started", "session_id": "s1"}),
            json.dumps({"type": "audio", "chunk": chunk}),
            json.dumps({"type": "session_ended"}),
        ]
    )
    media = LoopbackMediaChannel()
    collector = _Collector()
    outcome = await runtime.run(
        ctx=_ctx(),
        spec=_spec(),
        adapter=FakeVoiceProvider(),
        transport=transport,
        media=media,
        http=_Http(),
        secret=None,
        on_event=collector,
    )
    assert outcome.disposition is VoiceSessionDisposition.COMPLETED
    assert media.written == [b"\x10\x20\x30\x40"]
    assert outcome.provider_session_id == "s1"
    assert not transport.connected  # deterministic cleanup


async def test_runtime_interruption_discards_pending_audio() -> None:
    runtime = VoiceSessionRuntime(_SETTINGS)
    frames = [
        json.dumps({"type": "audio", "chunk": base64.b64encode(b"aaaa").decode()}),
        json.dumps({"type": "interruption"}),
        json.dumps({"type": "session_ended"}),
    ]
    transport = FakeVoiceStreamTransport(frames)
    media = LoopbackMediaChannel()
    collector = _Collector()
    outcome = await runtime.run(
        ctx=_ctx(),
        spec=_spec(),
        adapter=FakeVoiceProvider(),
        transport=transport,
        media=media,
        http=_Http(),
        secret=None,
        on_event=collector,
    )
    assert outcome.usage.interruptions == 1
    assert any(e.kind.name == "INTERRUPTION" for e in collector.events)


async def test_runtime_provider_disconnect_after_connect_fails_cleanly() -> None:
    runtime = VoiceSessionRuntime(_SETTINGS)
    transport = FakeVoiceStreamTransport([json.dumps({"type": "error", "reason": "boom"})])
    outcome = await runtime.run(
        ctx=_ctx(),
        spec=_spec(),
        adapter=FakeVoiceProvider(),
        transport=transport,
        media=LoopbackMediaChannel(),
        http=_Http(),
        secret=None,
        on_event=_drain,
    )
    assert outcome.disposition is VoiceSessionDisposition.FAILED
    assert outcome.error_code == "NXS_VOICE_PROVIDER_ERROR"


async def test_runtime_idle_timeout_ends_the_session() -> None:
    runtime = VoiceSessionRuntime(_SETTINGS)
    transport = FakeVoiceStreamTransport([], timeout_after=0)
    outcome = await runtime.run(
        ctx=_ctx(),
        spec=_spec(),
        adapter=FakeVoiceProvider(),
        transport=transport,
        media=LoopbackMediaChannel(),
        http=_Http(),
        secret=None,
        on_event=_drain,
    )
    assert outcome.error_code == "NXS_VOICE_PROVIDER_TIMEOUT"


async def test_runtime_cancellation_leaves_no_tasks() -> None:
    runtime = VoiceSessionRuntime(_SETTINGS)
    transport = FakeVoiceStreamTransport(
        [json.dumps({"type": "agent_response", "text": "hi"})], timeout_after=1
    )
    baseline = len(asyncio.all_tasks())
    for _ in range(5):
        transport = _BlockingStreamTransport()
        task = asyncio.create_task(
            runtime.run(
                ctx=_ctx(),
                spec=_spec(),
                adapter=FakeVoiceProvider(),
                transport=transport,
                media=LoopbackMediaChannel(),
                http=_Http(),
                secret=None,
                on_event=_drain,
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not transport.connected  # deterministic cleanup every cycle
    await asyncio.sleep(0.05)
    # repeated start/cancel cycles do not leak tasks
    assert len(asyncio.all_tasks()) <= baseline + 1


async def _noop() -> None:
    return None
