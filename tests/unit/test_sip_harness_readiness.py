"""Resolver harness readiness requires verified HTTPS and fail-closed routing."""

import asyncio
import socket
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tests.integration.sip_readiness import wait_resolver_ready
from tests.integration.sip_tls import resolver_certificate

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("trusted", [True, False])
async def test_resolver_readiness_requires_verified_listener(tmp_path: Path, trusted: bool) -> None:
    certificate, key = resolver_certificate(tmp_path, "127.0.0.1")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    other_certificate, _ = resolver_certificate(foreign, "127.0.0.1")
    app = FastAPI()
    accepted = asyncio.Event()

    async def reject_unauthenticated() -> JSONResponse:
        accepted.set()
        return JSONResponse({"status": "denied"}, status_code=403)

    app.add_api_route("/internal/sip/inbound", reject_unauthenticated, methods=["POST"])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            lifespan="off",
            ws="none",
            log_level="error",
            interface="asgi3",
            ssl_certfile=str(certificate),
            ssl_keyfile=str(key),
        )
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        if trusted:
            await wait_resolver_ready(server, serving, "127.0.0.1", port, certificate)
            assert server.started and accepted.is_set() and not serving.done()
        else:
            with pytest.raises(TimeoutError):
                await wait_resolver_ready(
                    server, serving, "127.0.0.1", port, other_certificate, timeout=0.5
                )
            assert not accepted.is_set()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        listener.close()


@pytest.mark.parametrize("crash", [False, True])
async def test_resolver_readiness_fails_on_server_exit(tmp_path: Path, crash: bool) -> None:
    certificate, _ = resolver_certificate(tmp_path, "127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(FastAPI(), interface="asgi3"))

    async def exit_server() -> None:
        if crash:
            raise ValueError("startup failed")

    serving = asyncio.create_task(exit_server())
    with pytest.raises(ValueError if crash else RuntimeError, match=r"startup failed|exited"):
        await wait_resolver_ready(server, serving, "127.0.0.1", 1, certificate)
    assert serving.done()


async def test_resolver_started_flag_has_bounded_watchdog(tmp_path: Path) -> None:
    certificate, _ = resolver_certificate(tmp_path, "127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(FastAPI(), interface="asgi3"))
    serving = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(TimeoutError):
            await wait_resolver_ready(server, serving, "127.0.0.1", 1, certificate, timeout=0.1)
        assert not serving.done()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
