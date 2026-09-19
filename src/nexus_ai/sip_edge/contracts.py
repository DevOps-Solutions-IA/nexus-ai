"""Bounded SIP control contracts; request data never supplies tenant authority."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from nexus_ai.cells.contracts import Generation, ReasonCode, SafeKey

Component = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$")
]
E164 = Annotated[str, StringConstraints(pattern=r"^\+[1-9][0-9]{6,14}$", max_length=16)]


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transport(StrEnum):
    UDP = "UDP"
    TCP = "TCP"
    TLS = "TLS"


class TargetState(StrEnum):
    REGISTERED = "REGISTERED"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    RETIRED = "RETIRED"


class RouteState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    ISSUED = "ISSUED"
    ESTABLISHED = "ESTABLISHED"
    ENDED = "ENDED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    EXPIRED = "EXPIRED"


class PermitState(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    CONSUMED = "CONSUMED"
    ENDED = "ENDED"
    AMBIGUOUS = "AMBIGUOUS"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


def fingerprint(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def private_target(value: str) -> str:
    if "%" in value:
        raise ValueError("scoped target addresses are forbidden")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("target must be a canonical private unicast IP") from None
    networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    if str(address) != value or not any(address in network for network in networks):
        raise ValueError("target must be a canonical private unicast IP")
    return value


class RegisterTarget(StrictContract):
    cell_id: UUID
    host: Annotated[str, StringConstraints(min_length=2, max_length=45)]
    port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    transport: Transport
    expected_revision: Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_806)]
    idempotency_key: SafeKey
    reason_code: ReasonCode

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return private_target(value)


class TargetMutation(StrictContract):
    cell_id: UUID
    target_id: UUID
    expected_revision: Generation
    state: TargetState
    idempotency_key: SafeKey
    reason_code: ReasonCode


class SipTransaction(StrictContract):
    call_id: Component
    from_tag: Component
    cseq: Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
    method: str = "INVITE"
    via_branch: Component
    via_sent_by: Component

    @field_validator("method")
    @classmethod
    def initial_invite(cls, value: str) -> str:
        if value != "INVITE":
            raise ValueError("only an initial INVITE can obtain a new route")
        return value

    @field_validator("via_branch")
    @classmethod
    def branch_cookie(cls, value: str) -> str:
        if not value.startswith("z9hG4bK") or len(value) <= 7:
            raise ValueError("RFC3261 transaction branch required")
        return value

    def digest(self, peer_id: UUID, direction: str) -> str:
        return fingerprint(
            {"peer_id": str(peer_id), "direction": direction, **self.model_dump(mode="json")}
        )


class InboundRequest(StrictContract):
    peer_id: UUID
    called_number: E164
    ingress_host: Annotated[
        str, StringConstraints(min_length=1, max_length=253, pattern=r"^[a-z0-9.-]+$")
    ]
    transaction: SipTransaction


class IssueRequest(StrictContract):
    route_id: UUID
    transaction: SipTransaction


class Page(StrictContract):
    after_id: UUID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
