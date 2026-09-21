"""Actual TCP/TLS SIP transport and certificate authority, never protocol mocks."""

import asyncio
import json
import secrets
import socket
import ssl
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from cryptography.fernet import Fernet
from sqlalchemy import text

from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.api import RouteHandles, create_resolver_app
from nexus_ai.sip_edge.authentication import EdgeAuthenticator
from nexus_ai.sip_edge.contracts import RegisterTarget, TargetMutation
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerPolicy, PeerProfile
from nexus_ai.sip_edge.security import EdgeCredential
from nexus_ai.sip_edge.targets import TargetNetworkPolicy, TargetRegistry
from tests.integration.sip_tls import SipPKI, resolver_certificate
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control
from tests.integration.test_sip_wire import docker, start_edge

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def read_sip(reader: asyncio.StreamReader) -> bytes:
    async with asyncio.timeout(5):
        headers = await reader.readuntil(b"\r\n\r\n")
        lengths = [
            line.split(b":", 1)[1].strip()
            for line in headers.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        ]
        assert len(lengths) == 1
        return headers + await reader.readexactly(int(lengths[0]))


def values(packet: bytes, name: str) -> list[str]:
    return [
        line.split(":", 1)[1].strip()
        for line in packet.decode().split("\r\n")
        if line.lower().startswith(name.lower() + ":")
    ]


def reply(packet: bytes, contact: str, status: str = "200 OK") -> bytes:
    headers = [
        line
        for line in packet.decode().split("\r\n")
        if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:", "record-route:"))
    ]
    headers = [
        line + ";tag=target" if line.lower().startswith("to:") and ";tag=" not in line else line
        for line in headers
    ]
    return (
        f"SIP/2.0 {status}\r\n"
        + "\r\n".join(headers)
        + f"\r\nContact: <{contact}>\r\nContent-Length: 0\r\n\r\n"
    ).encode()


class StreamUAS:
    def __init__(self) -> None:
        self.received: asyncio.Queue[tuple[bytes, asyncio.StreamWriter]] = asyncio.Queue()
        self.writers: list[asyncio.StreamWriter] = []
        self.tasks: set[asyncio.Task[Any]] = set()

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        try:
            while True:
                await self.received.put((await read_sip(reader), writer))
        except asyncio.IncompleteReadError, ConnectionError, TimeoutError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self) -> None:
        for writer in self.writers:
            writer.close()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
@pytest.mark.parametrize(
    ("transport", "attack"),
    [
        (transport, attack)
        for transport in ("TCP", "TLS")
        for attack in (
            "none",
            "cancel",
            "redirect",
            "untrusted_peer",
            "unsupported_method",
            "preloaded_route",
            "forged_authority",
            "downgrade_udp",
        )
    ]
    + [
        ("TLS", attack)
        for attack in (
            "wrong_client_pin",
            "untrusted_client_ca",
            "missing_client_certificate",
            "expired_client",
            "wrong_peer_policy",
            "wrong_target_pin",
            "untrusted_target_ca",
            "expired_target",
            "downgrade_tcp",
        )
    ],
)
async def test_real_stream_dialog_and_two_edge_authority(
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
    tmp_path: Path,
    transport: str,
    architecture: str,
    attack: str,
) -> None:
    cell, actor = target_control
    organization = await make_organization()
    _, number = await provision(telephony_stack, organization, "+12025550731")
    await CellPlacementService(tenant_database, event_platform.publisher).mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    gateway = json.loads(await docker("network", "inspect", "bridge"))[0]["IPAM"]["Config"][0][
        "Gateway"
    ]
    pki = SipPKI(tmp_path / "sip")
    pki.issue("identity", "127.0.0.1")
    client_certificate, client_key, client_pin = pki.issue(
        "carrier", gateway, expired=attack == "expired_client"
    )
    target_certificate, target_key, target_pin = pki.issue(
        "target", gateway, expired=attack == "expired_target"
    )
    if attack == "untrusted_client_ca":
        client_certificate, client_key, client_pin = SipPKI(tmp_path / "foreign-client").issue(
            "carrier", gateway
        )
    if attack == "untrusted_target_ca":
        target_certificate, target_key, target_pin = SipPKI(tmp_path / "foreign-target").issue(
            "target", gateway
        )
    configured_client_pin = secrets.token_hex(32) if attack == "wrong_client_pin" else client_pin
    configured_target_pin = secrets.token_hex(32) if attack == "wrong_target_pin" else target_pin
    server_tls = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH, cafile=str(pki.directory / "ca.pem")
    )
    server_tls.verify_mode = ssl.CERT_REQUIRED
    server_tls.load_cert_chain(target_certificate, target_key)
    client_tls = ssl.create_default_context(cafile=str(pki.directory / "ca.pem"))
    if attack != "missing_client_certificate":
        client_tls.load_cert_chain(client_certificate, client_key)
    uas = StreamUAS()
    target_server = await asyncio.start_server(
        uas.accept, gateway, 0, ssl=server_tls if transport == "TLS" else None
    )
    target_port = target_server.sockets[0].getsockname()[1]
    caller_tls = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH, cafile=str(pki.directory / "ca.pem")
    )
    caller_tls.verify_mode = ssl.CERT_REQUIRED
    caller_tls.load_cert_chain(client_certificate, client_key)
    caller_uas = StreamUAS()
    caller_server = await asyncio.start_server(
        caller_uas.accept, gateway, 0, ssl=caller_tls if transport == "TLS" else None
    )
    caller_port = caller_server.sockets[0].getsockname()[1]
    registry = TargetRegistry(
        tenant_database, TargetNetworkPolicy((f"{gateway}/32",), frozenset({target_port}))
    )
    target = await registry.register(
        actor,
        RegisterTarget(
            cell_id=cell,
            host=gateway,
            port=target_port,
            transport=transport,
            certificate_sha256=configured_target_pin if transport == "TLS" else None,
            expected_revision=0,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    await registry.transition(
        actor,
        TargetMutation(
            cell_id=cell,
            target_id=target.target_id,
            expected_revision=1,
            state="ACTIVE",
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    credentials = [EdgeCredential(uuid4(), secrets.token_bytes(32)) for _ in range(2)]
    peer = PeerProfile(
        peer_id=uuid4(),
        direction="INBOUND",
        edge_ids=tuple(c.edge_id for c in credentials),
        networks=(f"{gateway}/32",),
        transport=transport,
        isolated_network=transport != "TLS",
        certificate_sha256=(
            secrets.token_hex(32) if attack == "wrong_peer_policy" else configured_client_pin
        )
        if transport == "TLS"
        else None,
        ingress_hosts=("ingress.test",),
    )
    await PeerRegistry(tenant_database).register(actor, peer, expected_revision=0)
    app = create_resolver_app(
        EdgeAuthenticator(tenant_database, tuple(credentials)),
        PeerPolicy((peer,)),
        InboundRoutes(tenant_database, DidLocator(discovery_database)),
        RouteHandles(Fernet.generate_key()),
    )
    listener = socket.socket()
    listener.bind((gateway, 0))
    listener.listen(32)
    certificate_path, key_path = resolver_certificate(tmp_path, gateway)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            lifespan="off",
            interface="asgi3",
            ws="none",
            ssl_certfile=str(certificate_path),
            ssl_keyfile=str(key_path),
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    names: list[str] = []
    clients: list[asyncio.StreamWriter] = []
    scheme = "sips" if transport == "TLS" else "sip"
    target_contact = f"{scheme}:target@{gateway}:{target_port};transport={transport.lower()}"
    try:
        for index, credential in enumerate(credentials):
            configuration = tmp_path / f"edge-{index}.json"
            configuration.write_text(
                json.dumps(
                    {
                        "edge_id": str(credential.edge_id),
                        "hmac_secret": credential.secret.hex(),
                        "resolver_host": gateway,
                        "resolver_port": listener.getsockname()[1],
                        "isolated_test_network": True,
                        "edge_hosts": ["127.0.0.1"],
                        "limits": {
                            "messages_per_second": 100,
                            "pending_resolvers": 2,
                            "dialogs": 100,
                        },
                        "peers": [
                            {
                                "id": str(peer.peer_id),
                                "network": "10.254.254.254/32"
                                if attack == "untrusted_peer"
                                else f"{gateway}/32",
                                "direction": "INBOUND",
                                "ingress_hosts": ["ingress.test"],
                                "transport": transport,
                                "certificate_sha256": configured_client_pin
                                if transport == "TLS"
                                else None,
                                "contact_port": caller_port,
                            }
                        ],
                        "tls_targets": [
                            {
                                "host": gateway,
                                "port": target_port,
                                "certificate_sha256": configured_target_pin,
                            },
                            {
                                "host": gateway,
                                "port": caller_port,
                                "certificate_sha256": configured_client_pin,
                            },
                        ]
                        if transport == "TLS"
                        else [],
                    }
                )
            )
            configuration.chmod(0o644)
            name = "nxs-p19-stream-" + uuid4().hex
            names.append(name)
            image = (
                "nexus-p19-kamailio:6.1.4-development"
                if architecture == "amd64"
                else "nexus-p19-kamailio:6.1.4-arm64-wolfi-development"
            )
            edge_configuration = await asyncio.to_thread(Path("infrastructure/kamailio").resolve)
            address, _ = await start_edge(name, configuration, edge_configuration, image=image)
            try:
                reader, writer = await asyncio.open_connection(
                    address,
                    5061 if transport == "TLS" else 5060,
                    ssl=client_tls if transport == "TLS" else None,
                    server_hostname="127.0.0.1" if transport == "TLS" else None,
                    local_addr=(gateway, 0),
                )
            except ssl.SSLError, ConnectionError:
                assert attack in {
                    "untrusted_client_ca",
                    "missing_client_certificate",
                    "expired_client",
                }
                assert uas.received.empty()
                continue
            clients.append(writer)
            call_id = uuid4().hex
            branch = "z9hG4bK" + uuid4().hex

            def request(
                method: str,
                sequence: int,
                uri: str,
                routes: list[str],
                *,
                established: bool,
                initial_branch: str = branch,
                identity: str = call_id,
            ) -> bytes:
                transaction_branch = "z9hG4bK" + uuid4().hex if established else initial_branch
                return (
                    f"{method} {uri} SIP/2.0\r\n"
                    f"Via: SIP/2.0/{transport} {gateway}:{caller_port};"
                    f"branch={transaction_branch};rport\r\n"
                    "Max-Forwards: 70\r\nFrom: <sip:+12025550100@carrier.test>;tag=caller\r\n"
                    f"To: <sip:{number.e164}@ingress.test>"
                    + (";tag=target" if established else "")
                    + "\r\n"
                    f"Call-ID: {identity}\r\nCSeq: {sequence} {method}\r\n"
                    f"Contact: <{scheme}:caller@{gateway}:{caller_port};"
                    f"transport={transport.lower()}>\r\n"
                    + "".join(f"Route: {route}\r\n" for route in routes)
                    + "Content-Length: 0\r\n\r\n"
                ).encode()

            invite = request(
                "INVITE",
                1,
                f"{scheme}:{number.e164}@ingress.test;transport={transport.lower()}",
                [],
                established=False,
            )
            if attack == "none":
                invite = invite.replace(
                    b"Max-Forwards:",
                    b"X-NXS-Transport: UDP\r\nX-Cell-ID: forged\r\nMax-Forwards:",
                )
            elif attack == "preloaded_route":
                invite = invite.replace(
                    b"Max-Forwards:", b"Route: <sip:10.0.0.9:5060;lr>\r\nMax-Forwards:"
                )
            elif attack == "forged_authority":
                invite = invite.replace(b"@ingress.test", b"@untrusted.test").replace(
                    b"Max-Forwards:", b"X-NXS-Cell: forged\r\nX-NXS-Transport: UDP\r\nMax-Forwards:"
                )
            elif attack.startswith("downgrade_"):
                replacement = attack.removeprefix("downgrade_").encode()
                invite = invite.replace(b"sips:", b"sip:").replace(
                    b"transport=" + transport.lower().encode(), b"transport=" + replacement
                )
            elif attack == "unsupported_method":
                invite = invite.replace(b"INVITE", b"REGISTER")
            writer.write(invite)
            await writer.drain()
            if attack not in {"none", "cancel", "redirect"}:
                try:
                    response = await read_sip(reader)
                    while response.startswith(b"SIP/2.0 100"):
                        response = await read_sip(reader)
                    assert response.startswith((b"SIP/2.0 4", b"SIP/2.0 5")), response
                except ssl.SSLError, ConnectionError, asyncio.IncompleteReadError:
                    assert attack in {
                        "untrusted_client_ca",
                        "missing_client_certificate",
                        "expired_client",
                    }
                assert uas.received.empty(), "unauthorized target traffic"
                continue
            async with asyncio.timeout(5):
                packet, target_writer = await uas.received.get()
            assert packet.startswith(b"INVITE ")
            assert b"X-NXS-" not in packet and b"X-Cell-ID" not in packet
            assert values(packet, "Via")[0].startswith(f"SIP/2.0/{transport}")
            assert (target_writer.get_extra_info("ssl_object") is not None) == (transport == "TLS")
            if attack == "redirect":
                target_writer.write(
                    reply(
                        packet,
                        f"{scheme}:evil@{gateway}:{caller_port};transport={transport.lower()}",
                        "302 Moved Temporarily",
                    )
                )
                await target_writer.drain()
                response = await read_sip(reader)
                while response.startswith(b"SIP/2.0 100"):
                    response = await read_sip(reader)
                assert response.startswith(b"SIP/2.0 302")
                async with asyncio.timeout(5):
                    acknowledgment, _ = await uas.received.get()
                assert acknowledgment.startswith(b"ACK ")
                assert caller_uas.received.empty()
                continue
            if attack == "cancel":
                target_writer.write(reply(packet, target_contact, "180 Ringing"))
                await target_writer.drain()
                response = await read_sip(reader)
                while response.startswith(b"SIP/2.0 100"):
                    response = await read_sip(reader)
                assert response.startswith(b"SIP/2.0 180")
                writer.write(
                    request(
                        "CANCEL",
                        1,
                        f"{scheme}:{number.e164}@ingress.test;transport={transport.lower()}",
                        [],
                        established=False,
                    )
                )
                await writer.drain()
                async with asyncio.timeout(5):
                    cancelled, cancel_writer = await uas.received.get()
                assert cancelled.startswith(b"CANCEL ")
                assert values(cancelled, "Via")[0].startswith(f"SIP/2.0/{transport}")
                cancel_writer.write(reply(cancelled, target_contact))
                target_writer.write(reply(packet, target_contact, "487 Request Terminated"))
                await target_writer.drain()
                statuses = [await read_sip(reader), await read_sip(reader)]
                assert any(status.startswith(b"SIP/2.0 487") for status in statuses)
                async with asyncio.timeout(5):
                    acknowledgment, _ = await uas.received.get()
                assert acknowledgment.startswith(b"ACK ")
                continue
            provisional = reply(packet, target_contact, "183 Session Progress").replace(
                b"Content-Length: 0", b"Require: 100rel\r\nRSeq: 1\r\nContent-Length: 0"
            )
            target_writer.write(provisional)
            await target_writer.drain()
            progress = await read_sip(reader)
            while progress.startswith(b"SIP/2.0 100"):
                progress = await read_sip(reader)
            assert progress.startswith(b"SIP/2.0 183")
            early_contact = values(progress, "Contact")[0].strip("<>")
            early_routes = list(reversed(values(progress, "Record-Route")))
            prack = request("PRACK", 2, early_contact, early_routes, established=True).replace(
                b"Content-Length: 0", b"RAck: 1 1 INVITE\r\nContent-Length: 0"
            )
            writer.write(prack)
            await writer.drain()
            async with asyncio.timeout(5):
                prack_packet, prack_writer = await uas.received.get()
            assert prack_packet.startswith(b"PRACK ")
            assert values(prack_packet, "Via")[0].startswith(f"SIP/2.0/{transport}")
            prack_writer.write(reply(prack_packet, target_contact))
            await prack_writer.drain()
            assert (await read_sip(reader)).startswith(b"SIP/2.0 200")
            target_writer.write(reply(packet, target_contact))
            await target_writer.drain()
            response = await read_sip(reader)
            while response.startswith(b"SIP/2.0 100"):
                response = await read_sip(reader)
            assert response.startswith(b"SIP/2.0 200")
            remote = values(response, "Contact")[0].strip("<>")
            routes = list(reversed(values(response, "Record-Route")))
            assert routes
            origin_contact = values(packet, "Contact")[0].strip("<>")
            reverse_routes = values(packet, "Record-Route")
            reverse_request = (
                f"INFO {origin_contact} SIP/2.0\r\n"
                f"Via: SIP/2.0/{transport} {gateway}:{target_port};"
                f"branch=z9hG4bK{uuid4().hex};rport\r\n"
                f"From: <sip:{number.e164}@ingress.test>;tag=target\r\n"
                "To: <sip:+12025550100@carrier.test>;tag=caller\r\n"
                f"Call-ID: {call_id}\r\nCSeq: 10 INFO\r\nMax-Forwards: 70\r\n"
                + "".join(f"Route: {route}\r\n" for route in reverse_routes)
                + "Content-Length: 0\r\n\r\n"
            ).encode()
            target_writer.write(reverse_request)
            await target_writer.drain()
            async with asyncio.timeout(5):
                reverse_packet, reverse_writer = await caller_uas.received.get()
            assert reverse_packet.startswith(b"INFO ")
            assert values(reverse_packet, "Via")[0].startswith(f"SIP/2.0/{transport}")
            reverse_writer.write(reply(reverse_packet, origin_contact))
            await reverse_writer.drain()
            async with asyncio.timeout(5):
                reverse_response, _ = await uas.received.get()
            assert reverse_response.startswith(b"SIP/2.0 200")
            for downgrade in ["UDP", "TCP"] if transport == "TLS" else ["UDP"]:
                writer.write(
                    request(
                        "UPDATE",
                        2,
                        f"sip:target@{gateway}:{target_port};transport={downgrade.lower()}",
                        routes,
                        established=True,
                    )
                )
                await writer.drain()
                denied = await read_sip(reader)
                assert denied.startswith(b"SIP/2.0 403")
                assert uas.received.empty()
            if credential == credentials[-1]:
                replacement_target = await registry.register(
                    actor,
                    RegisterTarget(
                        cell_id=cell,
                        host=gateway,
                        port=target_port,
                        transport="UDP",
                        expected_revision=2,
                        idempotency_key=uuid4().hex,
                        reason_code="ROTATE",
                    ),
                    uuid4(),
                )
                await registry.transition(
                    actor,
                    TargetMutation(
                        cell_id=cell,
                        target_id=replacement_target.target_id,
                        expected_revision=3,
                        state="ACTIVE",
                        idempotency_key=uuid4().hex,
                        reason_code="ROTATE",
                    ),
                    uuid4(),
                )
                await CellPlacementService(tenant_database, event_platform.publisher).mutate(
                    organization.id,
                    actor,
                    PlacementMutation(
                        cell_id=cell,
                        operation="SUSPEND",
                        expected_generation=1,
                        idempotency_key=uuid4().hex,
                        reason_code="TEST",
                    ),
                    uuid4(),
                )
            for method, sequence in [
                ("ACK", 1),
                ("INVITE", 3),
                ("ACK", 3),
                ("UPDATE", 4),
                ("INFO", 5),
                ("BYE", 6),
            ]:
                writer.write(request(method, sequence, remote, routes, established=True))
                await writer.drain()
                async with asyncio.timeout(5):
                    dialog_packet, dialog_writer = await uas.received.get()
                assert dialog_packet.startswith(method.encode() + b" ")
                assert values(dialog_packet, "Via")[0].startswith(f"SIP/2.0/{transport}")
                if method != "ACK":
                    dialog_writer.write(reply(dialog_packet, target_contact))
                    await dialog_writer.drain()
                    response = await read_sip(reader)
                    while response.startswith(b"SIP/2.0 100"):
                        response = await read_sip(reader)
                    assert response.startswith(b"SIP/2.0 200")
                if method == "UPDATE":
                    writer.close()
                    await writer.wait_closed()
                    reader, writer = await asyncio.open_connection(
                        address,
                        5061 if transport == "TLS" else 5060,
                        ssl=client_tls if transport == "TLS" else None,
                        server_hostname="127.0.0.1" if transport == "TLS" else None,
                        local_addr=(gateway, 0),
                    )
                    clients.append(writer)
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            rows = (
                await tenant.session.execute(
                    text("SELECT cell_id, target_id, state FROM sip_route_authorizations")
                )
            ).all()
            assert len(rows) == (
                2
                if attack
                in {
                    "none",
                    "cancel",
                    "redirect",
                    "wrong_target_pin",
                    "untrusted_target_ca",
                    "expired_target",
                }
                else 0
            )
            assert attack != "none" or all(
                row.cell_id == cell and row.target_id == target.target_id and row.state == "ENDED"
                for row in rows
            )
    finally:
        for writer in clients:
            writer.close()
            with suppress(ssl.SSLError, ConnectionError):
                await writer.wait_closed()
        for name in names:
            await docker("rm", "-f", name)
        target_server.close()
        caller_server.close()
        await target_server.wait_closed()
        await caller_server.wait_closed()
        await uas.close()
        await caller_uas.close()
        server.should_exit = True
        await serving
