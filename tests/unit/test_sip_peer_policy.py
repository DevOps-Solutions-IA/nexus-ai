"""Peer observations must come from the authenticated edge's transport context."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.sip_edge.errors import SipRouteDeniedError
from nexus_ai.sip_edge.peers import PeerObservation, PeerPolicy, PeerProfile


def profile(**changes: object) -> PeerProfile:
    values = dict(
        peer_id=uuid4(),
        direction="INBOUND",
        edge_ids=(uuid4(),),
        networks=("10.20.0.0/24",),
        transport="UDP",
        isolated_network=True,
        ingress_hosts=("sip.example.test",),
    )
    return PeerProfile.model_validate(values | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"networks": ("0.0.0.0/0",)},
        {"networks": ("::/0",)},
        {"isolated_network": False},
        {"transport": "TLS"},
        {"ingress_hosts": ("bad\r\nhost",)},
        {"ingress_hosts": ("*.example.test",)},
        {"direction": "OUTBOUND"},
        {"cell_id": uuid4()},
    ],
)
def test_invalid_trust_configuration(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        profile(**changes)


def test_source_host_direction_and_edge_are_all_required() -> None:
    carrier = profile()
    policy = PeerPolicy((carrier,))
    observation = PeerObservation(source_address="10.20.0.7", transport="UDP")
    assert (
        policy.authenticate(
            carrier.edge_ids[0],
            carrier.peer_id,
            observation,
            direction="INBOUND",
            ingress_host="sip.example.test",
        )
        == carrier
    )
    for values in (
        dict(edge_id=uuid4()),
        dict(peer_id=uuid4()),
        dict(direction="OUTBOUND"),
        dict(ingress_host="attacker.test"),
        dict(observation=observation.model_copy(update={"source_address": "10.21.0.1"})),
    ):
        arguments = dict(
            edge_id=carrier.edge_ids[0],
            peer_id=carrier.peer_id,
            observation=observation,
            direction="INBOUND",
            ingress_host="sip.example.test",
        )
        with pytest.raises(SipRouteDeniedError):
            policy.authenticate(**(arguments | values))


def test_tls_requires_exact_transport_certificate() -> None:
    carrier = profile(transport="TLS", certificate_sha256="a" * 64)
    policy = PeerPolicy((carrier,))
    for pin in (None, "b" * 64):
        with pytest.raises(SipRouteDeniedError):
            policy.authenticate(
                carrier.edge_ids[0],
                carrier.peer_id,
                PeerObservation(
                    source_address="10.20.0.8", transport="TLS", certificate_sha256=pin
                ),
                direction="INBOUND",
                ingress_host="sip.example.test",
            )
