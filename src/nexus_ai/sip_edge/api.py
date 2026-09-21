"""Dedicated internal SIP resolver; never mounted on the tenant API implicitly."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr, ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from nexus_ai.core.errors import ConfigurationError, NxsError
from nexus_ai.core.problem_details import PROBLEM_MEDIA_TYPE, from_error
from nexus_ai.sip_edge.authentication import EdgeAuthenticator
from nexus_ai.sip_edge.contracts import (
    E164,
    InboundRequest,
    SipTransaction,
    StrictContract,
    fingerprint,
)
from nexus_ai.sip_edge.dialogs import DialogResult
from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.errors import SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.peers import PeerObservation, PeerPolicy


class InboundEnvelope(StrictContract):
    route: InboundRequest
    observed_peer: PeerObservation


class RouteHandle(StrictContract):
    direction: Literal["INBOUND", "OUTBOUND"] = "INBOUND"
    organization_id: UUID
    route_id: UUID
    edge_id: UUID
    boot_id: UUID
    transaction_digest: str
    origin_digest: str


class IssueEnvelope(StrictContract):
    handle: Annotated[str, Field(min_length=100, max_length=2048)]


class ResultEnvelope(IssueEnvelope):
    result: DialogResult


class EgressEnvelope(StrictContract):
    token: SecretStr
    destination: E164
    transaction: SipTransaction
    peer_id: UUID
    observed_peer: PeerObservation


class RouteHandles:
    def __init__(self, key: bytes) -> None:
        self._cipher = Fernet(key)

    def encode(self, handle: RouteHandle) -> str:
        return self._cipher.encrypt(handle.model_dump_json().encode()).decode("ascii")

    def decode(self, value: str) -> RouteHandle:
        try:
            return RouteHandle.model_validate_json(self._cipher.decrypt(value.encode("ascii")))
        except InvalidToken, ValidationError, UnicodeError:
            raise SipRouteDeniedError() from None


def create_resolver_app(
    authenticator: EdgeAuthenticator,
    peers: PeerPolicy,
    routes: InboundRoutes,
    handles: RouteHandles,
    egress: EgressPermits | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    async def dispatch(request: Request) -> JSONResponse:
        try:
            async with asyncio.timeout(2):
                if request.url.query:
                    raise SipRouteDeniedError()
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 8192:
                        raise SipRouteDeniedError()
                required = (
                    "x-nxs-edge",
                    "x-nxs-boot",
                    "x-nxs-timestamp",
                    "x-nxs-nonce",
                    "x-nxs-signature",
                )
                if any(len(request.headers.getlist(name)) != 1 for name in required):
                    raise SipRouteDeniedError()
                verified = await authenticator.authenticate(
                    edge_id=UUID(request.headers["x-nxs-edge"]),
                    boot_id=UUID(request.headers["x-nxs-boot"]),
                    method=request.method,
                    path=request.url.path,
                    body=bytes(body),
                    timestamp=int(request.headers["x-nxs-timestamp"]),
                    nonce=request.headers["x-nxs-nonce"],
                    signature=request.headers["x-nxs-signature"],
                )
                if request.url.path == "/internal/sip/inbound":
                    envelope = InboundEnvelope.model_validate_json(body)
                    peer = peers.authenticate(
                        verified.edge_id,
                        envelope.route.peer_id,
                        envelope.observed_peer,
                        direction="INBOUND",
                        ingress_host=envelope.route.ingress_host,
                    )
                    route = await routes.authorize(
                        envelope.route,
                        edge_id=verified.edge_id,
                        boot_id=verified.boot_id,
                        peer_revision=await routes.peer_snapshot(peer),
                    )
                    handle = handles.encode(
                        RouteHandle(
                            organization_id=route.organization_id,
                            route_id=route.route_id,
                            edge_id=verified.edge_id,
                            boot_id=verified.boot_id,
                            transaction_digest=envelope.route.transaction.digest(
                                envelope.route.peer_id, "INBOUND"
                            ),
                            origin_digest=fingerprint(
                                {
                                    "call": envelope.route.transaction.call_id,
                                    "from": envelope.route.transaction.from_tag,
                                }
                            ),
                        )
                    )
                    return JSONResponse({"route": route.model_dump(mode="json"), "handle": handle})
                if request.url.path == "/internal/sip/egress":
                    if egress is None:
                        raise SipRouteDeniedError()
                    outbound = EgressEnvelope.model_validate_json(body)
                    decision = await egress.consume(
                        outbound.token,
                        outbound.destination,
                        outbound.transaction,
                        edge_id=verified.edge_id,
                        boot_id=verified.boot_id,
                        peer_id=outbound.peer_id,
                        observation=outbound.observed_peer,
                    )
                    handle = handles.encode(
                        RouteHandle(
                            direction="OUTBOUND",
                            organization_id=decision.organization_id,
                            route_id=decision.permit_id,
                            edge_id=verified.edge_id,
                            boot_id=verified.boot_id,
                            transaction_digest=outbound.transaction.digest(
                                outbound.peer_id, "OUTBOUND"
                            ),
                            origin_digest=fingerprint(
                                {
                                    "call": outbound.transaction.call_id,
                                    "from": outbound.transaction.from_tag,
                                }
                            ),
                        )
                    )
                    return JSONResponse(decision.model_dump(mode="json") | {"handle": handle})
                envelope_issue = (
                    ResultEnvelope.model_validate_json(body)
                    if request.url.path == "/internal/sip/result"
                    else IssueEnvelope.model_validate_json(body)
                )
                handle_data = handles.decode(envelope_issue.handle)
                if (handle_data.edge_id, handle_data.boot_id) != (
                    verified.edge_id,
                    verified.boot_id,
                ):
                    raise SipRouteDeniedError()
                if isinstance(envelope_issue, ResultEnvelope):
                    observed = envelope_issue.result
                    if (
                        fingerprint({"call": observed.call_id, "from": observed.from_tag})
                        != handle_data.origin_digest
                    ):
                        raise SipRouteDeniedError()
                    recorder: Callable[
                        [UUID, UUID, UUID, UUID, str, DialogResult], Awaitable[str]
                    ] = routes.record_result
                    if handle_data.direction == "OUTBOUND":
                        if egress is None:
                            raise SipRouteDeniedError()
                        recorder = egress.record_result
                    state = await recorder(
                        handle_data.organization_id,
                        handle_data.route_id,
                        verified.edge_id,
                        verified.boot_id,
                        handle_data.transaction_digest,
                        observed,
                    )
                    return JSONResponse({"state": state})
                if handle_data.direction != "INBOUND":
                    raise SipRouteDeniedError()
                granted = await routes.issue(
                    handle_data.organization_id,
                    handle_data.route_id,
                    edge_id=verified.edge_id,
                    boot_id=verified.boot_id,
                    transaction_digest=handle_data.transaction_digest,
                )
                return JSONResponse({"initial_relay_granted": granted})
        except TimeoutError, DatabaseTimeoutError, DBAPIError, ConfigurationError:
            error: NxsError = SipRouteUnavailableError()
        except NxsError as caught:
            error = caught
        except ValueError, ValidationError, json.JSONDecodeError:
            error = SipRouteDeniedError()
        return JSONResponse(
            from_error(error).model_dump(exclude_none=True),
            status_code=error.status,
            media_type=PROBLEM_MEDIA_TYPE,
        )

    app.add_api_route("/internal/sip/inbound", dispatch, methods=["POST"])
    app.add_api_route("/internal/sip/issue", dispatch, methods=["POST"])
    app.add_api_route("/internal/sip/egress", dispatch, methods=["POST"])
    app.add_api_route("/internal/sip/result", dispatch, methods=["POST"])
    return app
