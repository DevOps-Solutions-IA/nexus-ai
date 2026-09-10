"""WebsocketVoiceStreamTransport — every websockets-library outcome is normalized to a
stable Nexus error / StreamClosed, and close never raises (NXS-P12, ADR-0088)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus_ai.voice.errors import (
    VoiceConnectionFailedError,
    VoiceProtocolError,
    VoiceProviderTimeoutError,
)
from nexus_ai.voice.transport import StreamClosed, WebsocketVoiceStreamTransport

pytestmark = pytest.mark.anyio


class _FakeWs:
    def __init__(self, *, recv_result: Any = "frame", raise_on: str | None = None) -> None:
        self._recv_result = recv_result
        self._raise_on = raise_on
        self.closed_with: int | None = None

    async def send(self, message: Any) -> None:
        if self._raise_on == "send":
            raise ConnectionResetError("peer gone")

    async def recv(self) -> Any:
        if self._raise_on == "recv":
            raise ConnectionResetError("peer gone")
        if self._raise_on == "recv_hang":
            await asyncio.Event().wait()
        return self._recv_result

    async def close(self, *, code: int = 1000) -> None:
        if self._raise_on == "close":
            raise RuntimeError("close blew up")
        self.closed_with = code


def _patch_connect(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> None:
    async def _connect(*args: Any, **kwargs: Any) -> Any:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("websockets.asyncio.client.connect", _connect)


async def test_connect_send_recv_close_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _FakeWs(recv_result="hello")
    _patch_connect(monkeypatch, ws)
    t = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    await t.connect(url="wss://x", headers={"a": "b"}, open_timeout=1)
    assert t.connected
    await t.send("ping")
    assert await t.recv(timeout=1) == "hello"
    await t.aclose(code=1000)
    assert not t.connected and ws.closed_with == 1000


async def test_connect_timeout_and_error_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connect(monkeypatch, TimeoutError())
    t = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    with pytest.raises(VoiceConnectionFailedError):
        await t.connect(url="wss://x", headers={}, open_timeout=1)

    _patch_connect(monkeypatch, OSError("dns failure"))
    with pytest.raises(VoiceConnectionFailedError):
        await t.connect(url="wss://x", headers={}, open_timeout=1)


async def test_send_and_recv_failures_surface_as_stream_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_connect(monkeypatch, _FakeWs(raise_on="send"))
    t = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    await t.connect(url="wss://x", headers={}, open_timeout=1)
    with pytest.raises(StreamClosed):
        await t.send("x")

    _patch_connect(monkeypatch, _FakeWs(raise_on="recv"))
    t2 = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    await t2.connect(url="wss://x", headers={}, open_timeout=1)
    with pytest.raises(StreamClosed):
        await t2.recv(timeout=1)


async def test_recv_idle_timeout_and_bad_frame_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_connect(monkeypatch, _FakeWs(raise_on="recv_hang"))
    t = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    await t.connect(url="wss://x", headers={}, open_timeout=1)
    with pytest.raises(VoiceProviderTimeoutError):
        await t.recv(timeout=0.05)

    _patch_connect(monkeypatch, _FakeWs(recv_result=123))
    t2 = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    await t2.connect(url="wss://x", headers={}, open_timeout=1)
    with pytest.raises(VoiceProtocolError):
        await t2.recv(timeout=1)


async def test_close_never_raises_and_send_before_connect_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_connect(monkeypatch, _FakeWs(raise_on="close"))
    t = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    await t.connect(url="wss://x", headers={}, open_timeout=1)
    await t.aclose()  # must not raise
    assert not t.connected

    fresh = WebsocketVoiceStreamTransport(max_message_bytes=1024)
    with pytest.raises(StreamClosed):
        await fresh.send("x")
    with pytest.raises(StreamClosed):
        await fresh.recv(timeout=1)
    await fresh.aclose()  # idempotent, no-op
