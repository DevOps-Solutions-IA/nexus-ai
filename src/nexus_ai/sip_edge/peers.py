"""Operations-configured peer identity, independent of SIP presentation headers."""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from nexus_ai.sip_edge.contracts import StrictContract, Transport
from nexus_ai.sip_edge.errors import SipRouteDeniedError


class PeerProfile(StrictContract):
    peer_id: UUID
    direction: Literal["INBOUND", "OUTBOUND"]
    edge_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=128)]
    networks: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    transport: Transport
    isolated_network: bool = False
    certificate_sha256: Annotated[str, StringConstraints(pattern="^[a-f0-9]{64}$")] | None = None
    ingress_hosts: Annotated[tuple[str, ...], Field(max_length=32)] = ()
    cell_id: UUID | None = None

    @model_validator(mode="after")
    def trusted_configuration(self) -> PeerProfile:
        networks = tuple(ip_network(value, strict=True) for value in self.networks)
        if any(network.prefixlen == 0 for network in networks):
            raise ValueError("wildcard peer trust is forbidden")
        if self.transport == Transport.TLS:
            if self.certificate_sha256 is None:
                raise ValueError("TLS peer certificate pin is required")
        elif not self.isolated_network or self.certificate_sha256 is not None:
            raise ValueError("UDP/TCP require explicitly isolated network trust")
        if self.direction == "OUTBOUND" and (self.cell_id is None or self.ingress_hosts):
            raise ValueError("outbound peer requires one server-configured Cell")
        if self.direction == "INBOUND" and (not self.ingress_hosts or self.cell_id is not None):
            raise ValueError("carrier requires explicit ingress hosts, not a Cell")
        for host in self.ingress_hosts:
            if (
                len(host) > 253
                or host != host.lower()
                or any(
                    not label
                    or len(label) > 63
                    or label.startswith("-")
                    or label.endswith("-")
                    or any(
                        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
                        for character in label
                    )
                    for label in host.split(".")
                )
            ):
                raise ValueError("invalid canonical ingress host")
        return self


class PeerObservation(StrictContract):
    source_address: Annotated[str, StringConstraints(max_length=45)]
    transport: Transport
    certificate_sha256: Annotated[str, StringConstraints(pattern="^[a-f0-9]{64}$")] | None = None


class PeerPolicy:
    def __init__(self, profiles: tuple[PeerProfile, ...]) -> None:
        if not 1 <= len(profiles) <= 256:
            raise ValueError("bounded peer profiles required")
        self._profiles = {profile.peer_id: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("duplicate peer profile")
        self._networks: dict[UUID, tuple[IPv4Network | IPv6Network, ...]] = {
            profile.peer_id: tuple(ip_network(value, strict=True) for value in profile.networks)
            for profile in profiles
        }

    def authenticate(
        self,
        edge_id: UUID,
        peer_id: UUID,
        observation: PeerObservation,
        *,
        direction: str,
        ingress_host: str | None = None,
    ) -> PeerProfile:
        profile = self._profiles.get(peer_id)
        try:
            address = ip_address(observation.source_address)
        except ValueError:
            raise SipRouteDeniedError() from None
        if (
            profile is None
            or profile.direction != direction
            or edge_id not in profile.edge_ids
            or observation.transport != profile.transport
            or not any(address in network for network in self._networks[peer_id])
            or (
                profile.transport == Transport.TLS
                and observation.certificate_sha256 != profile.certificate_sha256
            )
            or (direction == "INBOUND" and ingress_host not in profile.ingress_hosts)
        ):
            raise SipRouteDeniedError()
        return profile
