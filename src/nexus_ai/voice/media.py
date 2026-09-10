"""Media channels for the voice runtime (NXS-P12, ADR-0088).

A :class:`~nexus_ai.voice.runtime.MediaChannel` is the NXS-P11 / Asterisk side of the
real-time loop. P12 ships:

* :class:`LoopbackMediaChannel` — an in-memory channel for tests and the ``fake``
  provider path; audio the provider returns can be fed straight back in.
* :class:`GatewayMediaChannel` — the production seam. It carries a validated
  :class:`~nexus_ai.voice.bridge.MediaBridgePlan` and, in this phase, performs no live
  Asterisk / RTP I/O: the operations media gateway owns the socket. Until a gateway is
  provisioned it closes immediately, so a voice session started against a real bridge
  completes cleanly rather than hanging. Wiring the gateway transport is a documented
  follow-up and does not change any P12 contract.
"""

from __future__ import annotations

import asyncio
from collections import deque

from nexus_ai.voice.bridge import MediaBridgePlan
from nexus_ai.voice.transport import StreamClosed


class LoopbackMediaChannel:
    """Deterministic in-memory media channel. ``feed`` queues an inbound frame (from the
    "caller"); ``written`` collects everything the provider sent to the media side."""

    def __init__(self, inbound: list[bytes] | None = None) -> None:
        self._inbound: deque[bytes] = deque(inbound or ())
        self._event = asyncio.Event()
        if self._inbound:
            self._event.set()
        self._closed = False
        self.written: list[bytes] = []

    def feed(self, frame: bytes) -> None:
        self._inbound.append(frame)
        self._event.set()

    async def read(self, *, timeout: float) -> bytes:
        while not self._inbound:
            if self._closed:
                raise StreamClosed("the media channel is closed")
            self._event.clear()
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        frame = self._inbound.popleft()
        if not self._inbound:
            self._event.clear()
        return frame

    def write(self, frame: bytes) -> None:
        if not self._closed:
            self.written.append(frame)

    async def aclose(self) -> None:
        self._closed = True
        self._event.set()


class GatewayMediaChannel:
    """The production media seam. Holds the validated bridge plan; performs no live I/O
    in this phase (the ops media gateway owns the RTP / external-media socket)."""

    def __init__(self, plan: MediaBridgePlan) -> None:
        self.plan = plan
        self._closed = False

    async def read(self, *, timeout: float) -> bytes:
        del timeout
        # No local gateway transport in this phase — signal end-of-stream so the runtime
        # completes deterministically instead of blocking on a socket that is not wired.
        raise StreamClosed("no media-gateway transport is configured for this deployment")

    def write(self, frame: bytes) -> None:
        del frame  # dropped: the gateway is not wired in this phase

    async def aclose(self) -> None:
        self._closed = True
