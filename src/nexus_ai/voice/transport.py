"""Bounded real-time voice transport (NXS-P12, ADR-0088).

Every real-time path in P12 is bounded and cancellation-safe:

* :class:`BoundedFrameQueue` — a fixed-depth queue with an explicit per-frame byte
  ceiling and a non-blocking producer that applies backpressure (drop-oldest) rather
  than growing without limit;
* :class:`VoiceStreamTransport` — the provider-neutral duplex byte transport the
  provider adapters talk through, with an explicit open / receive / close timeout on
  every call and deterministic cleanup;
* :class:`WebsocketVoiceStreamTransport` — the production implementation over the
  already-vendored ``websockets`` library, every knob bounded;
* :class:`FakeVoiceStreamTransport` — a deterministic scripted transport for tests.

No unbounded ``asyncio.Queue``. No blocking network I/O. No task or socket left behind
after ``aclose``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Iterable
from typing import Protocol

from nexus_ai.voice.errors import (
    VoiceConnectionFailedError,
    VoiceProtocolError,
    VoiceProviderTimeoutError,
)


class StreamClosed(Exception):
    """The transport was closed (locally or by the peer). Not an error by itself."""

    def __init__(
        self, message: str = "the voice stream is closed", *, by_peer: bool = False
    ) -> None:
        super().__init__(message)
        self.by_peer = by_peer


class BoundedFrameQueue:
    """A fixed-depth async queue of ``bytes`` frames. A frame larger than
    ``max_frame_bytes`` is rejected; a ``put`` on a full queue drops the OLDEST frame
    (backpressure) and records the drop — it never blocks a producer or grows."""

    def __init__(self, *, depth: int, max_frame_bytes: int) -> None:
        self._depth = depth
        self._max_frame_bytes = max_frame_bytes
        self._items: deque[bytes] = deque()
        self._not_empty = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def put(self, frame: bytes) -> None:
        if self._closed:
            raise StreamClosed("cannot enqueue on a closed queue")
        if len(frame) > self._max_frame_bytes:
            raise VoiceProtocolError("an audio frame exceeded the configured size ceiling")
        if len(self._items) >= self._depth:
            self._items.popleft()
            self.dropped += 1
        self._items.append(frame)
        self._not_empty.set()

    async def get(self, *, timeout: float) -> bytes:
        while not self._items:
            if self._closed:
                raise StreamClosed("the queue is drained and closed")
            self._not_empty.clear()
            try:
                await asyncio.wait_for(self._not_empty.wait(), timeout=timeout)
            except TimeoutError as exc:
                raise VoiceProviderTimeoutError("no audio frame within the idle window") from exc
        frame = self._items.popleft()
        if not self._items:
            self._not_empty.clear()
        return frame

    def drain_nowait(self) -> list[bytes]:
        items = list(self._items)
        self._items.clear()
        self._not_empty.clear()
        return items

    def clear(self) -> int:
        """Discard every queued frame (used on a barge-in). Returns the discard count."""
        count = len(self._items)
        self._items.clear()
        self._not_empty.clear()
        return count

    def close(self) -> None:
        self._closed = True
        self._not_empty.set()

    @property
    def depth(self) -> int:
        return len(self._items)


class VoiceStreamTransport(Protocol):
    """A provider-neutral duplex byte transport. Text and binary frames both surface as
    ``bytes`` / ``str`` from :meth:`recv`. Every method is bounded by an explicit
    timeout and is safe to call after close."""

    async def connect(self, *, url: str, headers: dict[str, str], open_timeout: float) -> None: ...

    async def send(self, message: bytes | str) -> None: ...

    async def recv(self, *, timeout: float) -> bytes | str: ...

    async def aclose(self, *, code: int = 1000) -> None: ...

    @property
    def connected(self) -> bool: ...


class WebsocketVoiceStreamTransport:
    """Production transport over the vendored ``websockets`` asyncio client. Bounds the
    open timeout, close timeout, max message size and keepalive ping/timeout. Holds no
    background task of its own beyond what the library manages, and closes deterministically.
    """

    def __init__(self, *, max_message_bytes: int, close_timeout: float = 5.0) -> None:
        self._max_message_bytes = max_message_bytes
        self._close_timeout = close_timeout
        self._ws: object | None = None

    async def connect(self, *, url: str, headers: dict[str, str], open_timeout: float) -> None:
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError as exc:  # pragma: no cover - dependency is vendored + pinned
            raise VoiceConnectionFailedError("the websocket client is unavailable") from exc
        try:
            self._ws = await ws_connect(
                url,
                additional_headers=headers,
                open_timeout=open_timeout,
                close_timeout=self._close_timeout,
                max_size=self._max_message_bytes,
                ping_interval=20,
                ping_timeout=20,
            )
        except TimeoutError as exc:
            raise VoiceConnectionFailedError("the voice websocket handshake timed out") from exc
        except VoiceConnectionFailedError:
            raise
        except Exception as exc:
            # websockets raises many subclasses (WebSocketException, OSError, ...); every
            # one is a connection failure to us and is normalized to one stable code.
            raise VoiceConnectionFailedError("the voice websocket could not be opened") from exc

    async def send(self, message: bytes | str) -> None:
        if self._ws is None:
            raise StreamClosed("send on a transport that is not connected")
        try:
            await self._ws.send(message)  # type: ignore[attr-defined]
        except Exception as exc:
            raise StreamClosed("the websocket rejected a send", by_peer=True) from exc

    async def recv(self, *, timeout: float) -> bytes | str:
        if self._ws is None:
            raise StreamClosed("recv on a transport that is not connected")
        try:
            message = await asyncio.wait_for(self._ws.recv(), timeout=timeout)  # type: ignore[attr-defined]
        except TimeoutError as exc:
            raise VoiceProviderTimeoutError("no provider frame within the idle window") from exc
        except VoiceProtocolError:
            raise
        except Exception as exc:
            raise StreamClosed("the websocket closed during recv", by_peer=True) from exc
        if isinstance(message, bytes | str):
            return message
        raise VoiceProtocolError("the provider sent a frame of an unexpected type")

    async def aclose(self, *, code: int = 1000) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        with contextlib.suppress(Exception):  # close must never raise
            await asyncio.wait_for(ws.close(code=code), timeout=self._close_timeout)  # type: ignore[attr-defined]

    @property
    def connected(self) -> bool:
        return self._ws is not None


class FakeVoiceStreamTransport:
    """A deterministic in-memory transport for tests. Scripts a sequence of inbound
    frames and can inject a peer close, a timeout or a malformed frame at a chosen point.
    Records every sent message."""

    def __init__(
        self,
        inbound: Iterable[bytes | str] | None = None,
        *,
        close_after: int | None = None,
        timeout_after: int | None = None,
        hold: bool = False,
    ) -> None:
        self._inbound: deque[bytes | str] = deque(inbound or ())
        self._close_after = close_after
        self._timeout_after = timeout_after
        self._hold = hold
        self._recv_count = 0
        self._connected = False
        self.sent: list[bytes | str] = []
        self.closed_code: int | None = None
        self.connect_calls = 0

    def push(self, frame: bytes | str) -> None:
        self._inbound.append(frame)

    async def connect(self, *, url: str, headers: dict[str, str], open_timeout: float) -> None:
        del url, headers, open_timeout
        self.connect_calls += 1
        self._connected = True

    async def send(self, message: bytes | str) -> None:
        if not self._connected:
            raise StreamClosed("send before connect")
        self.sent.append(message)

    async def recv(self, *, timeout: float) -> bytes | str:
        del timeout
        if not self._connected:
            raise StreamClosed("recv before connect")
        self._recv_count += 1
        if self._timeout_after is not None and self._recv_count > self._timeout_after:
            raise VoiceProviderTimeoutError("scripted idle timeout")
        if self._close_after is not None and self._recv_count > self._close_after:
            raise StreamClosed("scripted peer close", by_peer=True)
        if not self._inbound:
            if self._hold:
                await asyncio.Event().wait()  # stays live until cancelled
            raise StreamClosed("scripted stream exhausted", by_peer=True)
        return self._inbound.popleft()

    async def aclose(self, *, code: int = 1000) -> None:
        self._connected = False
        self.closed_code = code

    @property
    def connected(self) -> bool:
        return self._connected
