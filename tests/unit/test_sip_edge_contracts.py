"""Fail-closed request syntax without network or placement authority."""

from __future__ import annotations

from ipaddress import IPv4Address
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.sip_edge.contracts import (
    InboundRequest,
    Page,
    RegisterTarget,
    SipTransaction,
    fingerprint,
    private_target,
)


def transaction(**changes: object) -> SipTransaction:
    return SipTransaction.model_validate(
        {
            "call_id": "call@example.test",
            "from_tag": "from-tag",
            "cseq": 1,
            "via_branch": "z9hG4bK-test",
            "via_sent_by": "edge.example.test:5060",
            **changes,
        }
    )


@pytest.mark.parametrize("host", ["10.12.0.2", "172.16.1.2", "192.168.2.1", "fd01::1"])
def test_private_canonical_targets(host: str) -> None:
    assert private_target(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "127.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "::1",
        "fe80::1",
        "ff02::1",
        str(IPv4Address(0)),
        "::",
        "224.0.0.1",
        "example.test",
        "sip:user@10.1.2.3",
        "10.1.2.3:5060",
        "10.1.2.3/path",
        "10.1.2.3?x=1",
        "010.1.2.3",
        "FD01::1",
        "fd01::1%eth0",
        "10.1.2.3\r\nRoute: evil",
    ],
)
def test_unsafe_target_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        private_target(host)


@pytest.mark.parametrize(
    "changes",
    [
        {"port": 80},
        {"port": 65536},
        {"port": True},
        {"transport": "WS"},
        {"expected_revision": -1},
        {"idempotency_key": "x" * 129},
        {"reason_code": "x\r\n"},
        {"cell_id": "bad"},
        {"sql": "SELECT"},
    ],
)
def test_target_request_bounds(changes: dict[str, object]) -> None:
    payload = {
        "cell_id": uuid4(),
        "host": "10.1.2.3",
        "port": 5060,
        "transport": "UDP",
        "expected_revision": 0,
        "idempotency_key": "register-1",
        "reason_code": "REGISTER",
        **changes,
    }
    with pytest.raises(ValidationError):
        RegisterTarget.model_validate(payload)


def test_transaction_digest_binds_more_than_call_id() -> None:
    peer = uuid4()
    initial = transaction().digest(peer, "INBOUND")
    for changes in (
        {"from_tag": "another"},
        {"cseq": 2},
        {"via_branch": "z9hG4bK-second"},
        {"via_sent_by": "other.example.test"},
        {"call_id": "different"},
    ):
        assert transaction(**changes).digest(peer, "INBOUND") != initial
    assert transaction().digest(uuid4(), "INBOUND") != initial
    assert transaction().digest(peer, "OUTBOUND") != initial
    assert transaction().digest(peer, "INBOUND") == initial
    assert fingerprint({"first": 1, "second": 2}) == fingerprint({"second": 2, "first": 1})


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "ACK"},
        {"method": "CANCEL"},
        {"method": "REGISTER"},
        {"cseq": 0},
        {"cseq": True},
        {"cseq": 2_147_483_648},
        {"call_id": "a" * 257},
        {"from_tag": "bad\r\n"},
        {"via_branch": "not-a-cookie"},
        {"via_branch": "z9hG4bK"},
    ],
)
def test_initial_transaction_bounds(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        transaction(**changes)


@pytest.mark.parametrize("field", ["organization_id", "cell_id", "host", "transport", "headers"])
def test_inbound_has_no_client_authority_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        InboundRequest.model_validate(
            {
                "peer_id": uuid4(),
                "called_number": "+12025550123",
                "ingress_host": "ingress.example.test",
                "transaction": transaction(),
                field: "forged",
            }
        )


@pytest.mark.parametrize("payload", [{"limit": 0}, {"limit": 101}, {"after_id": "bad"}])
def test_control_pages_are_bounded(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Page.model_validate(payload)
