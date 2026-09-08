"""The governed outbound HTTP executor (NXS-INT-001, ADR-0056).

This is the ONLY place in Nexus that opens an outbound HTTP connection to an
integration. Every call is bounded and validated:

* SSRF destination policy is enforced again here, at execution time — the host is
  re-resolved and every resolved address re-classified, then the connection is PINNED to
  a validated address (``sni_hostname`` keeps TLS correct), so a DNS rebind between
  validation and connect cannot land on a private address;
* bounded connect / read / write timeouts AND a hard total-time ceiling;
* a request-body size limit and a streamed response-body size limit (the response is
  never fully buffered before the limit check);
* TLS certificate verification is always on and cannot be disabled;
* redirects are disabled by default; when a bounded count is allowed each hop is
  re-validated through the destination policy;
* reserved/hop-by-hop headers are stripped and a fixed ``User-Agent`` is set;
* failures are mapped to a small typed set (:class:`FailureKind`) so the retry policy and
  the error taxonomy have a stable contract. No raw upstream body or header is ever
  logged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.core.logging import get_logger
from nexus_ai.integrations.backoff import FailureKind
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import RESERVED_REQUEST_HEADERS, DestinationRule
from nexus_ai.integrations.errors import (
    IntegrationConnectionError,
    IntegrationRedirectBlockedError,
    IntegrationResponseTooLargeError,
    IntegrationTimeoutError,
    IntegrationTlsError,
)


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    content_type: str | None = None
    timeout_seconds: float | None = None
    destination_rule: DestinationRule = field(default_factory=DestinationRule)


@dataclass(frozen=True, slots=True)
class RawResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    final_url: str


class ExecutorFailure(Exception):
    """Wraps a transport failure with its retry classification and public error."""

    def __init__(self, kind: FailureKind, public: Exception) -> None:
        super().__init__(str(public))
        self.kind = kind
        self.public = public


class GovernedHttpExecutor:
    def __init__(
        self,
        settings: IntegrationsSettings,
        policy: DestinationPolicy,
        *,
        client_factory: object | None = None,
    ) -> None:
        self._settings = settings
        self._policy = policy
        self._log = get_logger("nexus_ai.integrations.executor")
        # ``client_factory`` lets tests substitute a transport; production builds a fresh
        # bounded client per call so no ambient connection state is reused unsafely.
        self._client_factory = client_factory

    def _build_client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(timeout)  # type: ignore[operator, no-any-return]
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            verify=True,
            trust_env=False,
            max_redirects=0,
            headers={"User-Agent": self._settings.user_agent},
        )

    def _timeout(self, request: OutboundRequest) -> tuple[httpx.Timeout, float]:
        total = self._settings.total_timeout_seconds
        if request.timeout_seconds is not None:
            total = min(total, request.timeout_seconds)
        read = min(self._settings.read_timeout_seconds, total)
        connect = min(self._settings.connect_timeout_seconds, total)
        return httpx.Timeout(connect=connect, read=read, write=read, pool=connect), total

    def _sanitise_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in RESERVED_REQUEST_HEADERS:
                continue
            if "\n" in value or "\r" in value or "\n" in name or "\r" in name:
                raise IntegrationConnectionError("a request header value is malformed")
            clean[name] = value
        return clean

    async def send(self, request: OutboundRequest) -> RawResponse:
        if request.body is not None and len(request.body) > self._settings.max_request_bytes:
            raise IntegrationConnectionError("the request body exceeds the configured limit")
        timeout, total = self._timeout(request)
        headers = self._sanitise_headers(request.headers)
        if "authorization" in {k.lower() for k in request.headers}:
            headers["Authorization"] = next(
                v for k, v in request.headers.items() if k.lower() == "authorization"
            )
        if request.content_type and request.body is not None:
            headers.setdefault("Content-Type", request.content_type)
        headers.setdefault("User-Agent", self._settings.user_agent)

        max_hops = self._settings.max_redirects
        current_url = request.url
        started = time.monotonic()
        try:
            async with asyncio.timeout(total):
                client = self._build_client(timeout)
                async with client:
                    for hop in range(max_hops + 1):
                        response = await self._send_once(
                            client,
                            request.method,
                            current_url,
                            headers,
                            request.body,
                            request.destination_rule,
                        )
                        if response.status_code not in (301, 302, 303, 307, 308):
                            elapsed_ms = int((time.monotonic() - started) * 1000)
                            return RawResponse(
                                status_code=response.status_code,
                                headers={
                                    k.lower(): v
                                    for k, v in response.headers.items()
                                    if k.lower() not in RESERVED_REQUEST_HEADERS
                                },
                                body=await self._read_bounded(response),
                                elapsed_ms=elapsed_ms,
                                final_url=current_url,
                            )
                        location = response.headers.get("location")
                        if not location or hop >= max_hops:
                            raise IntegrationRedirectBlockedError(
                                "the upstream returned a redirect that is not allowed"
                            )
                        current_url = str(httpx.URL(current_url).join(location))
                    raise IntegrationRedirectBlockedError("redirect limit exceeded")
        except TimeoutError as exc:
            raise ExecutorFailure(
                FailureKind.IN_FLIGHT, IntegrationTimeoutError("the outbound request timed out")
            ) from exc

    async def _send_once(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        rule: DestinationRule,
    ) -> httpx.Response:
        resolved = self._policy.resolve(url, rule=rule)
        parts = urlsplit(url)
        pinned = resolved.addresses[0]
        host_for_url = f"[{pinned}]" if _is_ipv6(pinned) else pinned
        netloc = f"{host_for_url}:{resolved.port}"
        connect_url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        send_headers = dict(headers)
        send_headers["Host"] = (
            resolved.host if resolved.port in (80, 443) else f"{resolved.host}:{resolved.port}"
        )
        pre_send = FailureKind.PRE_SEND
        try:
            httpx_request = client.build_request(
                method,
                connect_url,
                headers=send_headers,
                content=body,
                extensions={"sni_hostname": resolved.host},
            )
            return await client.send(httpx_request, stream=True)
        except httpx.ConnectTimeout as exc:
            raise ExecutorFailure(
                pre_send, IntegrationTimeoutError("connection timed out")
            ) from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            if _looks_like_tls(exc):
                raise ExecutorFailure(
                    FailureKind.TERMINAL, IntegrationTlsError("TLS verification failed")
                ) from exc
            raise ExecutorFailure(
                pre_send, IntegrationConnectionError("could not connect to the upstream")
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ExecutorFailure(
                FailureKind.IN_FLIGHT, IntegrationTimeoutError("the upstream read timed out")
            ) from exc
        except httpx.HTTPError as exc:
            raise ExecutorFailure(
                FailureKind.IN_FLIGHT, IntegrationConnectionError("the outbound request failed")
            ) from exc

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        limit = self._settings.max_response_bytes
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise IntegrationResponseTooLargeError(
                        "the upstream response exceeds the configured size limit"
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ExecutorFailure(
                FailureKind.IN_FLIGHT,
                IntegrationConnectionError("the upstream connection dropped mid-response"),
            ) from exc
        finally:
            await response.aclose()
        return b"".join(chunks)


def _is_ipv6(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).version == 6
    except ValueError:  # pragma: no cover - resolver returns valid addresses
        return False


def _looks_like_tls(exc: Exception) -> bool:
    text = str(exc).lower()
    return "certificate" in text or "ssl" in text or "tls" in text
