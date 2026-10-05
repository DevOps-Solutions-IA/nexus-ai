"""Regression coverage for deterministic in-process Uvicorn startup."""

import asyncio
import socket

import pytest

from tests.integration.uvicorn_ready import start_uvicorn_server

pytestmark = pytest.mark.anyio


class ControlledServer:
    def __init__(self) -> None:
        self.started = False
        self.should_exit = False
        self.entered = asyncio.Event()
        self.release_startup = asyncio.Event()
        self.stop = asyncio.Event()

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        assert sockets == []
        self.entered.set()
        await self.release_startup.wait()
        self.started = True
        await self.stop.wait()


class FailingServer:
    started = False
    should_exit = False

    async def serve(self, *, sockets: list[socket.socket]) -> None:
        raise RuntimeError("startup failed")


async def test_waits_until_server_reports_started() -> None:
    server = ControlledServer()
    readiness = asyncio.create_task(start_uvicorn_server(server, sockets=[]))

    await server.entered.wait()
    await asyncio.sleep(0)
    assert not readiness.done()

    server.release_startup.set()
    server_task = await readiness
    assert server.started is True
    assert not server_task.done()

    server.stop.set()
    await server_task


async def test_propagates_startup_failure_without_returning_task() -> None:
    server = FailingServer()

    with pytest.raises(RuntimeError, match="startup failed"):
        await start_uvicorn_server(server, sockets=[])

    assert server.should_exit is True
