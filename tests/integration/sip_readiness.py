"""Functional startup barriers for disposable SIP harness dependencies."""

from __future__ import annotations

import asyncio
import ssl
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import uvicorn

# Match start_edge's existing 15s startup watchdog; this is harness startup budget,
# not a SIP transaction deadline or production SLA.
RESOLVER_STARTUP_WATCHDOG_SECONDS = 15


async def wait_resolver_ready(
    server: uvicorn.Server,
    serving: asyncio.Task[Any],
    host: str,
    port: int,
    certificate: Path,
    *,
    timeout: float = RESOLVER_STARTUP_WATCHDOG_SECONDS,
) -> None:
    """Require Uvicorn startup and verified TLS/HTTP on the real resolver route.

    The probe deliberately omits resolver authentication. A 403 proves the actual
    ASGI resolver route accepted certificate-verified HTTPS and failed closed before
    any tenant authority can be issued. Polling is restricted to startup; no SIP
    transaction is retried.
    """

    async def probe() -> None:
        context = ssl.create_default_context(cafile=str(certificate))
        async with httpx.AsyncClient(verify=context, trust_env=False, timeout=1) as client:
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
            while True:
                try:
                    response = await client.post(
                        f"https://{host}:{port}/internal/sip/inbound",
                        content=b"{}",
                    )
                except httpx.ConnectError, httpx.ConnectTimeout:
                    await asyncio.sleep(0.05)
                else:
                    assert response.status_code == 403, response.status_code
                    return

    probing = asyncio.create_task(probe())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait({serving, probing}, return_when=asyncio.FIRST_COMPLETED)
            if serving in done or serving.done():
                await serving
                raise RuntimeError("Resolver server exited before readiness")
            await probing
    finally:
        probing.cancel()
        with suppress(asyncio.CancelledError):
            await probing
