"""Bounded edge authentication; successful verification still requires replay arbitration."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from uuid import UUID

from nexus_ai.sip_edge.errors import SipRouteDeniedError

_NONCE = re.compile(r"^[a-f0-9]{64}$")
_SIGNATURE = re.compile(r"^[a-f0-9]{64}$")
_PATHS = frozenset(
    {"/internal/sip/inbound", "/internal/sip/issue", "/internal/sip/egress", "/internal/sip/result"}
)


@dataclass(frozen=True, slots=True)
class EdgeCredential:
    edge_id: UUID
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.edge_id, UUID) or len(self.secret) < 32:
            raise ValueError("invalid edge credential")


@dataclass(frozen=True, slots=True)
class VerifiedRequest:
    edge_id: UUID
    boot_id: UUID
    nonce_digest: str
    timestamp: int
    request_digest: str


def signing_payload(
    edge_id: UUID,
    boot_id: UUID,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> bytes:
    if (
        method != "POST"
        or path not in _PATHS
        or len(body) > 8192
        or not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
        or not 0 < timestamp < 10_000_000_000
        or not _NONCE.fullmatch(nonce)
    ):
        raise SipRouteDeniedError()
    return "\n".join(
        (
            "nxs-sip-v1",
            str(edge_id),
            str(boot_id),
            method,
            path,
            hashlib.sha256(body).hexdigest(),
            str(timestamp),
            nonce,
        )
    ).encode("ascii")


def verify_request(
    credential: EdgeCredential,
    *,
    boot_id: UUID,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
    signature: str,
    database_timestamp: int,
) -> VerifiedRequest:
    """Verify only; callers must atomically reject durable nonce reuse before dispatch."""
    payload = signing_payload(credential.edge_id, boot_id, method, path, body, timestamp, nonce)
    if (
        not _SIGNATURE.fullmatch(signature)
        or abs(database_timestamp - timestamp) > 30
        or not hmac.compare_digest(
            hmac.digest(credential.secret, payload, "sha256").hex(), signature
        )
    ):
        raise SipRouteDeniedError()
    return VerifiedRequest(
        edge_id=credential.edge_id,
        boot_id=boot_id,
        nonce_digest=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
        timestamp=timestamp,
        request_digest=hashlib.sha256(payload).hexdigest(),
    )
