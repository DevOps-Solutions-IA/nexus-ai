"""Request signatures bind every authority-bearing input, not tenant headers."""

import hmac
import secrets
from uuid import uuid4

import pytest

from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.security import EdgeCredential, signing_payload, verify_request


def test_valid_signed_request_and_secret_safe_repr() -> None:
    secret = secrets.token_bytes(32)
    credential = EdgeCredential(uuid4(), secret)
    boot = uuid4()
    nonce = secrets.token_hex(32)
    payload = signing_payload(
        credential.edge_id, boot, "POST", "/internal/sip/inbound", b"{}", 100, nonce
    )
    result = verify_request(
        credential,
        boot_id=boot,
        method="POST",
        path="/internal/sip/inbound",
        body=b"{}",
        timestamp=100,
        nonce=nonce,
        signature=hmac.digest(secret, payload, "sha256").hex(),
        database_timestamp=100,
    )
    assert result.edge_id == credential.edge_id
    assert result.boot_id == boot
    assert nonce not in repr(result)
    assert repr(secret) not in repr(credential)


@pytest.mark.parametrize(
    "change",
    [
        {"method": "GET"},
        {"path": "/internal/sip/egress"},
        {"path": "/internal/sip/inbound?organization=forged"},
        {"body": b'{"cell_id":"forged"}'},
        {"body": b"x" * 8193},
        {"timestamp": 101},
        {"timestamp": True},
        {"nonce": "z" * 64},
        {"nonce": "a" * 64},
        {"signature": "0" * 64},
        {"signature": "0" * 65},
        {"database_timestamp": 131},
        {"database_timestamp": 69},
        {"boot_id": uuid4()},
    ],
)
def test_changed_binding_or_expired_signature_denied(change: dict[str, object]) -> None:
    credential = EdgeCredential(uuid4(), secrets.token_bytes(32))
    boot = uuid4()
    nonce = secrets.token_hex(32)
    payload = signing_payload(
        credential.edge_id, boot, "POST", "/internal/sip/inbound", b"{}", 100, nonce
    )
    arguments = {
        "boot_id": boot,
        "method": "POST",
        "path": "/internal/sip/inbound",
        "body": b"{}",
        "timestamp": 100,
        "nonce": nonce,
        "signature": hmac.digest(credential.secret, payload, "sha256").hex(),
        "database_timestamp": 100,
    }
    arguments.update(change)
    with pytest.raises(SipRouteDeniedError):
        verify_request(credential, **arguments)


def test_distinct_edge_cannot_reuse_signature() -> None:
    secret = secrets.token_bytes(32)
    original = EdgeCredential(uuid4(), secret)
    other = EdgeCredential(uuid4(), secret)
    boot = uuid4()
    nonce = secrets.token_hex(32)
    payload = signing_payload(
        original.edge_id, boot, "POST", "/internal/sip/inbound", b"{}", 100, nonce
    )
    with pytest.raises(SipRouteDeniedError):
        verify_request(
            other,
            boot_id=boot,
            method="POST",
            path="/internal/sip/inbound",
            body=b"{}",
            timestamp=100,
            nonce=nonce,
            signature=hmac.digest(secret, payload, "sha256").hex(),
            database_timestamp=100,
        )


def test_short_secret_rejected() -> None:
    with pytest.raises(ValueError, match="invalid edge credential"):
        EdgeCredential(uuid4(), b"short")
