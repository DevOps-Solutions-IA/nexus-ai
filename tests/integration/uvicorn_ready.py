"""Readiness coordination for in-process Uvicorn integration servers."""

import asyncio
import socket
from collections.abc import Sequence
from contextlib import suppress
from typing import Protocol


class UvicornLikeServer(Protocol):
    started: bool
    should_exit: bool

    async def serve(self, *, sockets: list[socket.socket]) -> None: ...


async def start_uvicorn_server(
    server: UvicornLikeServer,
    *,
    sockets: Sequence[socket.socket],
    startup_timeout: float = 5.0,
) -> asyncio.Task[None]:
    """Start a server task and return only after Uvicorn reports readiness."""

    task = asyncio.create_task(server.serve(sockets=list(sockets)))
    try:
        async with asyncio.timeout(startup_timeout):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Uvicorn exited before reporting readiness")
                await asyncio.sleep(0)
    except BaseException:
        server.should_exit = True
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        raise
    return task
