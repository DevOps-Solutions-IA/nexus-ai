"""Resolver harness readiness requires verified HTTPS and fail-closed routing."""

import asyncio
import socket
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tests.integration.sip_readiness import wait_resolver_ready
from tests.integration.sip_tls import resolver_certificate

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("trusted", "hostname", "mode", "ready"),
    [
        (True, "127.0.0.1", "verified_identity", True),
        (False, "127.0.0.1", "verified_identity", False),
        (True, "127.0.0.2", "verified_identity", False),
        (True, "127.0.0.2", "tls_identity_mismatch", True),
        (False, "127.0.0.2", "tls_identity_mismatch", False),
    ],
)
async def test_resolver_readiness_requires_verified_listener(
    tmp_path: Path,
    trusted: bool,
    hostname: str,
    mode: Literal["verified_identity", "tls_identity_mismatch"],
    ready: bool,
) -> None:
    certificate, key = resolver_certificate(tmp_path, hostname)
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
        trust_certificate = certificate if trusted else other_certificate
        if ready:
            await wait_resolver_ready(
                server, serving, "127.0.0.1", port, trust_certificate, mode=mode
            )
            assert server.started and accepted.is_set() and not serving.done()
        else:
            with pytest.raises(TimeoutError):
                await wait_resolver_ready(
                    server, serving, "127.0.0.1", port, trust_certificate, mode=mode, timeout=0.5
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


@pytest.mark.parametrize("mode", ["verified_identity", "tls_identity_mismatch"])
async def test_resolver_started_flag_has_bounded_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["verified_identity", "tls_identity_mismatch"],
) -> None:
    certificate, _ = resolver_certificate(tmp_path, "127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(FastAPI(), interface="asgi3"))
    post = AsyncMock(side_effect=AssertionError("HTTP probe preceded server.started"))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    serving = asyncio.create_task(asyncio.Event().wait())
    try:
        with pytest.raises(TimeoutError):
            await wait_resolver_ready(
                server, serving, "127.0.0.1", 1, certificate, mode=mode, timeout=0.1
            )
        assert not serving.done()
        post.assert_not_awaited()
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
