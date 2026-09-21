"""Disposable non-root Kamailio exchanges actual SIP with a UDP UAS."""

import asyncio
import base64
import json
import secrets
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from cryptography.fernet import Fernet
from sqlalchemy import text

from nexus_ai.cells.contracts import PlacementMutation, RegisterCellRequest
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
from tests.integration.sip_tls import edge_tls_mounts, resolver_certificate
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_did_locator import provision
from tests.integration.test_sip_target_constraints import target_control as target_control

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def docker(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/docker",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode()[-2000:])
    return (stdout + stderr if arguments[0] == "logs" else stdout).decode().strip()


async def isolated_sender(
    packet: bytes, destination: str, *, network: str = "bridge", address: str | None = None
) -> str:
    program = (
        "import base64,socket,sys;"
        "client=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);client.bind(('0.0.0.0',5060));client.settimeout(2);"
        "client.sendto(base64.b64decode(sys.argv[2]),(sys.argv[1],5060));"
        "print(client.recvfrom(65536)[0].split(b'\\r\\n',1)[0].decode())"
    )
    return await docker(
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--network",
        network,
        *(("--ip", address) if address else ()),
        "--entrypoint",
        "/usr/bin/python",
        "nexus-p19-kamailio:6.1.4-development",
        "-c",
        program,
        destination,
        base64.b64encode(packet).decode(),
    )


def udp_socket(address: str) -> socket.socket:
    connection = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    connection.bind((address, 0))
    connection.setblocking(False)
    return connection


async def cancel_pending_invite(
    caller: socket.socket, uas: socket.socket, destination: tuple[str, int], request: bytes
) -> None:
    loop = asyncio.get_running_loop()
    await loop.sock_sendto(caller, request, destination)
    async with asyncio.timeout(5):
        invite, edge_address = await loop.sock_recvfrom(uas, 65536)
    assert invite.startswith(b"INVITE ")

    def reply(packet: bytes, status: str) -> bytes:
        headers = [
            line
            for line in packet.decode().split("\r\n")
            if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:"))
        ]
        headers = [
            line + ";tag=cancelled" if line.lower().startswith("to:") else line for line in headers
        ]
        return (
            f"SIP/2.0 {status}\r\n" + "\r\n".join(headers) + "\r\nContent-Length: 0\r\n\r\n"
        ).encode()

    await loop.sock_sendto(uas, reply(invite, "180 Ringing"), edge_address)
    async with asyncio.timeout(5):
        while True:
            response, _ = await loop.sock_recvfrom(caller, 65536)
            if response.startswith(b"SIP/2.0 180"):
                break
    cancel = request.replace(b"INVITE", b"CANCEL")
    await loop.sock_sendto(caller, cancel, destination)
    async with asyncio.timeout(5):
        while True:
            cancelled, _ = await loop.sock_recvfrom(uas, 65536)
            if cancelled.startswith(b"CANCEL "):
                break
    await loop.sock_sendto(uas, reply(cancelled, "200 OK"), edge_address)
    await loop.sock_sendto(uas, reply(invite, "487 Request Terminated"), edge_address)
    async with asyncio.timeout(5):
        while True:
            response, _ = await loop.sock_recvfrom(caller, 65536)
            if response.startswith(b"SIP/2.0 487"):
                break
    acknowledgment = request.replace(b"INVITE", b"ACK").replace(
        b"@ingress.test>\r\n", b"@ingress.test>;tag=cancelled\r\n"
    )
    await loop.sock_sendto(caller, acknowledgment, destination)
    async with asyncio.timeout(5):
        while True:
            packet, _ = await loop.sock_recvfrom(uas, 65536)
            if packet.startswith(b"ACK "):
                break


async def start_edge(
    name: str,
    configuration: Path,
    config_directory: Path,
    *,
    network: str | None = None,
    address: str | None = None,
    image: str = "nexus-p19-kamailio:6.1.4-development",
) -> tuple[str, int]:
    await docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        "NXS_SIP_TOPOLOGY_KEY=" + secrets.token_hex(32),
        *(
            ("--network", network, "--ip", address)
            if network is not None and address is not None
            else ()
        ),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        *edge_tls_mounts(configuration.parent),
        "--tmpfs",
        "/run/nxs:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
        "-v",
        f"{config_directory}:/etc/nxs:ro",
        "-v",
        f"{configuration}:/run/secrets/nxs-sip-edge.json:ro",
        "-v",
        f"{configuration.parent / 'resolver-ca.pem'}:/run/secrets/resolver-ca.pem:ro",
        image,
        "-DD",
        "-E",
        "-Y",
        "/run/nxs",
        "-f",
        "/etc/nxs/kamailio.cfg",
    )
    async with asyncio.timeout(15):
        logs = await asyncio.create_subprocess_exec(
            "/usr/bin/docker",
            "logs",
            "-f",
            name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert logs.stdout is not None
            while True:
                line = await logs.stdout.readline()
                if not line:
                    raise RuntimeError(await docker("logs", name))
                if b"nxs_edge_ready" in line:
                    break
        finally:
            if logs.returncode is None:
                logs.terminate()
            await logs.wait()
    container = json.loads(await docker("inspect", name))[0]
    return address or container["NetworkSettings"]["IPAddress"], 5060


@pytest.mark.parametrize("rotate_target", [False, True])
@pytest.mark.parametrize(
    "failure_mode",
    [
        "none",
        "bounded_delay",
        "tls",
        "issue_response_loss",
        "missing_target",
        "suspended",
        "database",
    ],
)
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
async def test_real_kamailio_invite_and_forged_route_zero_send(
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
    tmp_path: Path,
    rotate_target: bool,
    failure_mode: str,
    architecture: str,
) -> None:
    image = (
        "nexus-p19-kamailio:6.1.4-development"
        if architecture == "amd64"
        else "nexus-p19-kamailio:6.1.4-arm64-wolfi-development"
    )
    cell, actor = target_control
    organization = await make_organization()
    _, number = await provision(telephony_stack, organization, "+12025550999")
    await CellPlacementService(tenant_database, event_platform.publisher).mutate(
        organization.id,
        actor,
        PlacementMutation(
            cell_id=cell, operation="ASSIGN", idempotency_key=uuid4().hex, reason_code="TEST"
        ),
        uuid4(),
    )
    network = json.loads(await docker("network", "inspect", "bridge"))[0]
    gateway = network["IPAM"]["Config"][0]["Gateway"]
    uas, caller = udp_socket(gateway), udp_socket(gateway)
    replacement_uas = udp_socket(gateway)
    other_cell_uas = udp_socket(gateway)
    http_listener = socket.socket()
    http_listener.bind((gateway, 0))
    http_listener.listen(32)
    registry = TargetRegistry(
        tenant_database,
        TargetNetworkPolicy(
            (f"{gateway}/32",),
            frozenset({uas.getsockname()[1], replacement_uas.getsockname()[1]}),
        ),
    )
    target = await registry.register(
        actor,
        RegisterTarget(
            cell_id=cell,
            host=gateway,
            port=uas.getsockname()[1],
            transport="UDP",
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
            state="ACTIVE",
            expected_revision=1,
            idempotency_key=uuid4().hex,
            reason_code="TEST",
        ),
        uuid4(),
    )
    edge, second_edge, peer = uuid4(), uuid4(), uuid4()
    credential = EdgeCredential(edge, secrets.token_bytes(32))
    second_credential = EdgeCredential(second_edge, secrets.token_bytes(32))
    await PeerRegistry(tenant_database).register(
        actor,
        PeerProfile(
            peer_id=peer,
            direction="INBOUND",
            edge_ids=(edge, second_edge),
            networks=(f"{gateway}/32",),
            transport="UDP",
            isolated_network=True,
            ingress_hosts=("ingress.test",),
        ),
        expected_revision=0,
    )
    app = create_resolver_app(
        EdgeAuthenticator(tenant_database, (credential, second_credential)),
        PeerPolicy(
            (
                PeerProfile(
                    peer_id=peer,
                    direction="INBOUND",
                    edge_ids=(edge, second_edge),
                    networks=(f"{gateway}/32",),
                    transport="UDP",
                    isolated_network=True,
                    ingress_hosts=("ingress.test",),
                ),
            )
        ),
        InboundRoutes(tenant_database, DidLocator(discovery_database)),
        RouteHandles(Fernet.generate_key()),
    )
    issue_committed, release_response = asyncio.Event(), asyncio.Event()
    delayed_response = asyncio.Event()
    resolver_results: list[tuple[str, int]] = []

    @app.middleware("http")
    async def lose_first_committed_issue_response(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        resolver_results.append((request.url.path, response.status_code))
        if (
            failure_mode == "bounded_delay"
            and request.url.path == "/internal/sip/inbound"
            and response.status_code == 200
            and not delayed_response.is_set()
        ):
            asyncio.get_running_loop().call_later(0.9, delayed_response.set)
            await delayed_response.wait()
        if (
            failure_mode == "issue_response_loss"
            and request.url.path == "/internal/sip/issue"
            and response.status_code == 200
            and not issue_committed.is_set()
        ):
            issue_committed.set()
            await release_response.wait()
        return response

    certificate_path, key_path = await asyncio.to_thread(
        resolver_certificate, tmp_path, "127.0.0.2" if failure_mode == "tls" else gateway
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            lifespan="off",
            ws="none",
            interface="asgi3",
            ssl_certfile=str(certificate_path),
            ssl_keyfile=str(key_path),
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[http_listener]))
    configuration = tmp_path / "edge.json"
    configuration.write_text(
        json.dumps(
            {
                "edge_id": str(edge),
                "hmac_secret": credential.secret.hex(),
                "resolver_host": gateway,
                "resolver_port": http_listener.getsockname()[1],
                "isolated_test_network": True,
                "edge_hosts": ["127.0.0.1"],
                "limits": {"messages_per_second": 100, "pending_resolvers": 2, "dialogs": 100},
                "peers": [
                    {
                        "id": str(peer),
                        "network": f"{gateway}/32",
                        "direction": "INBOUND",
                        "ingress_hosts": ["ingress.test"],
                    }
                ],
            }
        )
    )
    configuration.chmod(0o644)
    name = "nxs-p19-wire-" + uuid4().hex
    second_name = "nxs-p19-wire-second-" + uuid4().hex
    second_configuration = tmp_path / "second-edge.json"
    confidential_marker = secrets.token_hex(32)
    loop = asyncio.get_running_loop()
    config_directory = await asyncio.to_thread(Path("infrastructure/kamailio").resolve)
    try:
        await docker(
            "run",
            "-d",
            "--name",
            name,
            "-e",
            "NXS_SIP_TOPOLOGY_KEY=" + secrets.token_hex(32),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *edge_tls_mounts(configuration.parent),
            "--tmpfs",
            "/run/nxs:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
            "-v",
            f"{config_directory}:/etc/nxs:ro",
            "-v",
            f"{configuration}:/run/secrets/nxs-sip-edge.json:ro",
            "-v",
            f"{certificate_path}:/run/secrets/resolver-ca.pem:ro",
            image,
            "-DD",
            "-E",
            "-Y",
            "/run/nxs",
            "-f",
            "/etc/nxs/kamailio.cfg",
        )
        container = json.loads(await docker("inspect", name))[0]
        destination = (container["NetworkSettings"]["IPAddress"], 5060)
        call_id = uuid4().hex
        request = (
            f"INVITE sip:{number.e164}@ingress.test SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {gateway}:{caller.getsockname()[1]};"
            f"branch=z9hG4bK{uuid4().hex};rport\r\n"
            "Max-Forwards: 70\r\n"
            "From: <sip:+12025550100@carrier.test>;tag=testtag\r\n"
            f"To: <sip:{number.e164}@ingress.test>\r\n"
            f"Call-ID: {call_id}\r\nCSeq: 1 INVITE\r\n"
            f"Contact: <sip:caller@{gateway}:{caller.getsockname()[1]}>\r\n"
            "Content-Length: 0\r\n\r\n"
        ).encode()
        async with asyncio.timeout(15):
            logs = await asyncio.create_subprocess_exec(
                "/usr/bin/docker",
                "logs",
                "-f",
                name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                assert logs.stdout is not None
                while True:
                    line = await logs.stdout.readline()
                    if not line:
                        raise RuntimeError(await docker("logs", name))
                    if b"nxs_edge_ready" in line:
                        break
            finally:
                if logs.returncode is None:
                    logs.terminate()
                await logs.wait()
        attacks = (
            (
                request.replace(
                    f"Contact: <sip:caller@{gateway}:{caller.getsockname()[1]}>".encode(),
                    f"Contact: <sip:caller@{gateway}:1>".encode(),
                ),
                b"403",
            ),
            (request.replace(b"Max-Forwards: 70", b"Max-Forwards: 0"), b"483"),
            (
                request.replace(
                    b"Content-Length: 0", b"Route: <sip:evil.invalid>\r\nContent-Length: 0"
                ),
                b"403",
            ),
            (request.replace(b"@ingress.test SIP/2.0", b"@evil.invalid SIP/2.0"), b"403"),
            (request.replace(b"INVITE", b"REGISTER"), b"405"),
            (request.replace(number.e164.encode(), b"+12025550001"), b"503"),
            (
                request.replace(b"Content-Length: 0", b"Content-Length: 0\r\nContent-Length: 1"),
                None,
            ),
            (request.replace(b"Content-Length: 0", b"l: 0\r\nContent-Length: 0"), None),
            (
                request.replace(
                    b"Content-Length: 0",
                    f"Authorization: Bearer {confidential_marker}\r\nContent-Length: 5".encode(),
                ),
                None,
            ),
            (
                request.replace(
                    b"Content-Length: 0", b"X-Large: " + b"a" * 2049 + b"\r\nContent-Length: 0"
                ),
                b"400",
            ),
            (
                request.replace(b"@ingress.test SIP/2.0", b"@ingress.test;transport=tcp SIP/2.0"),
                b"403",
            ),
        )
        for attack, status in attacks:
            await loop.sock_sendto(caller, attack, destination)
            try:
                async with asyncio.timeout(2):
                    rejected, _ = await loop.sock_recvfrom(caller, 65536)
                assert rejected.startswith(b"SIP/2.0 " + (status or b"400"))
            except TimeoutError:
                assert status is None
            with pytest.raises(BlockingIOError):
                uas.recvfrom(65536)
        container = json.loads(await docker("inspect", name))[0]
        assert container["Config"]["User"] == "10001:10001"
        assert container["HostConfig"]["ReadonlyRootfs"] is True
        assert container["HostConfig"]["Privileged"] is False
        assert container["HostConfig"]["CapDrop"] == ["ALL"]
        assert container["HostConfig"]["PidMode"] != "host"
        assert container["HostConfig"]["NetworkMode"] != "host"
        assert all("docker.sock" not in mount["Destination"] for mount in container["Mounts"])
        assert all(
            "DATABASE" not in value and "postgresql" not in value
            for value in container["Config"]["Env"]
        )
        assert "locator_dsn" not in json.loads(configuration.read_text())
        edge_address = container["NetworkSettings"]["IPAddress"]
        relay_attack = request.replace(b"@ingress.test SIP/2.0", b"@198.51.100.10 SIP/2.0")
        assert await isolated_sender(relay_attack, edge_address) == "SIP/2.0 403 Forbidden"
        with pytest.raises(BlockingIOError):
            uas.recvfrom(65536)
        health = request.replace(b"INVITE", b"OPTIONS").replace(
            b"@ingress.test SIP/2.0", b"@127.0.0.1 SIP/2.0"
        )
        await loop.sock_sendto(caller, health, destination)
        async with asyncio.timeout(2):
            health_reply, _ = await loop.sock_recvfrom(caller, 65536)
        assert health_reply.startswith(b"SIP/2.0 200 Edge alive")
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(text("SELECT count(*) FROM sip_route_authorizations"))
            ).scalar_one() == 0
        if failure_mode == "missing_target":
            await registry.transition(
                actor,
                TargetMutation(
                    cell_id=cell,
                    target_id=target.target_id,
                    state="DRAINING",
                    expected_revision=2,
                    idempotency_key=uuid4().hex,
                    reason_code="TEST",
                ),
                uuid4(),
            )
        elif failure_mode == "suspended":
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
        elif failure_mode == "database":
            await discovery_database.disconnect()
        await loop.sock_sendto(caller, request, destination)
        if failure_mode not in {"none", "bounded_delay"}:
            async with asyncio.timeout(2):
                rejected, _ = await loop.sock_recvfrom(caller, 65536)
            assert rejected.startswith(b"SIP/2.0 503")
            with pytest.raises(BlockingIOError):
                uas.recvfrom(65536)
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                states = (
                    (
                        await tenant.session.execute(
                            text("SELECT state FROM sip_route_authorizations")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert states == (["ISSUED"] if failure_mode == "issue_response_loss" else [])
            if failure_mode == "issue_response_loss":
                assert issue_committed.is_set()
                release_response.set()
                await loop.sock_sendto(caller, request, destination)
                async with asyncio.timeout(2):
                    replay, _ = await loop.sock_recvfrom(caller, 65536)
                assert replay.startswith(b"SIP/2.0 409")
                await docker("stop", "--time", "2", name)
                second_configuration.write_text(configuration.read_text())
                second_configuration.chmod(0o644)
                restarted = await start_edge(
                    second_name, second_configuration, config_directory, image=image
                )
                await loop.sock_sendto(caller, request, restarted)
                async with asyncio.timeout(2):
                    fenced, _ = await loop.sock_recvfrom(caller, 65536)
                assert fenced.startswith(b"SIP/2.0 503")
                with pytest.raises(BlockingIOError):
                    uas.recvfrom(65536)
            return
        async with asyncio.timeout(5):
            received, upstream_address = await loop.sock_recvfrom(uas, 65536)
        assert received.startswith(b"INVITE ")
        assert b"Record-Route:" in received
        assert b"X-NXS-" not in received
        received_lines = received.decode().split("\r\n")
        response_headers = [
            line
            for line in received_lines
            if line.lower().startswith(
                ("via:", "from:", "to:", "call-id:", "cseq:", "record-route:")
            )
        ]
        response_headers = [
            line + ";tag=uastag" if line.lower().startswith("to:") else line
            for line in response_headers
        ]
        response = (
            "SIP/2.0 200 OK\r\n"
            + "\r\n".join(response_headers)
            + f"\r\nContact: <sip:uas@{gateway}:{uas.getsockname()[1]}>"
            + "\r\nContent-Length: 0\r\n\r\n"
        ).encode()
        provisional = response.replace(b"SIP/2.0 200 OK", b"SIP/2.0 183 Session Progress").replace(
            b"Content-Length: 0", b"Require: 100rel\r\nRSeq: 1\r\nContent-Length: 0"
        )
        await loop.sock_sendto(uas, provisional, upstream_address)
        async with asyncio.timeout(5):
            while True:
                progress, _ = await loop.sock_recvfrom(caller, 65536)
                if progress.startswith(b"SIP/2.0 183"):
                    break
        await loop.sock_sendto(caller, request, destination)
        async with asyncio.timeout(2):
            retransmission_reply, _ = await loop.sock_recvfrom(caller, 65536)
        assert retransmission_reply.startswith(b"SIP/2.0 183")
        with pytest.raises(BlockingIOError):
            uas.recvfrom(65536)
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(text("SELECT count(*) FROM sip_route_authorizations"))
            ).scalar_one() == 1
        progress_lines = progress.decode().split("\r\n")
        progress_contact = next(
            line.split(":", 1)[1].strip().strip("<>")
            for line in progress_lines
            if line.lower().startswith("contact:")
        )
        progress_routes = [
            line.split(":", 1)[1].strip()
            for line in progress_lines
            if line.lower().startswith("record-route:")
        ]
        progress_routes.reverse()
        prack = (
            f"PRACK {progress_contact} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {gateway}:{caller.getsockname()[1]};"
            f"branch=z9hG4bK{uuid4().hex};rport\r\n"
            "Max-Forwards: 70\r\nFrom: <sip:+12025550100@carrier.test>;tag=testtag\r\n"
            f"To: <sip:{number.e164}@ingress.test>;tag=uastag\r\n"
            f"Call-ID: {call_id}\r\nCSeq: 2 PRACK\r\nRAck: 1 1 INVITE\r\n"
            + "".join(f"Route: {route}\r\n" for route in progress_routes)
            + "Content-Length: 0\r\n\r\n"
        ).encode()
        await loop.sock_sendto(caller, prack, destination)
        async with asyncio.timeout(5):
            while True:
                received_prack, prack_address = await loop.sock_recvfrom(uas, 65536)
                if received_prack.startswith(b"PRACK "):
                    break
        prack_headers = [
            line
            for line in received_prack.decode().split("\r\n")
            if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:"))
        ]
        await loop.sock_sendto(
            uas,
            (
                "SIP/2.0 200 OK\r\n" + "\r\n".join(prack_headers) + "\r\nContent-Length: 0\r\n\r\n"
            ).encode(),
            prack_address,
        )
        async with asyncio.timeout(5):
            while True:
                prack_response, _ = await loop.sock_recvfrom(caller, 65536)
                if prack_response.startswith(b"SIP/2.0 200") and b"CSeq: 2 PRACK" in prack_response:
                    break
        await loop.sock_sendto(uas, response, upstream_address)
        async with asyncio.timeout(5):
            while True:
                caller_response, _ = await loop.sock_recvfrom(caller, 65536)
                if caller_response.startswith(b"SIP/2.0 200"):
                    break
        route_set = [
            line.split(":", 1)[1].strip()
            for line in caller_response.decode().split("\r\n")
            if line.lower().startswith("record-route:")
        ]
        route_set.reverse()
        assert route_set
        contact = next(
            line.split(":", 1)[1].strip().strip("<>")
            for line in caller_response.decode().split("\r\n")
            if line.lower().startswith("contact:")
        )
        assert gateway not in contact
        caller_contact = next(
            line.split(":", 1)[1].strip().strip("<>")
            for line in received_lines
            if line.lower().startswith("contact:")
        )
        reverse_routes = [
            line.split(":", 1)[1].strip()
            for line in received_lines
            if line.lower().startswith("record-route:")
        ]
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(text("SELECT state FROM sip_route_authorizations"))
            ).scalar_one() == "ESTABLISHED"
            assert (
                await tenant.session.execute(text("SELECT count(*) FROM sip_dialog_bindings"))
            ).scalar_one() == 1
        placement_service = CellPlacementService(tenant_database, event_platform.publisher)
        await placement_service.mutate(
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
        if rotate_target:
            replacement = await registry.register(
                actor,
                RegisterTarget(
                    cell_id=cell,
                    host=gateway,
                    port=replacement_uas.getsockname()[1],
                    transport="UDP",
                    expected_revision=2,
                    idempotency_key=uuid4().hex,
                    reason_code="TEST",
                ),
                uuid4(),
            )
            await registry.transition(
                actor,
                TargetMutation(
                    cell_id=cell,
                    target_id=replacement.target_id,
                    state="ACTIVE",
                    expected_revision=3,
                    idempotency_key=uuid4().hex,
                    reason_code="TEST",
                ),
                uuid4(),
            )
        reverse_request = (
            f"INFO {caller_contact} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {gateway}:{uas.getsockname()[1]};"
            f"branch=z9hG4bK{uuid4().hex};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: <sip:{number.e164}@ingress.test>;tag=uastag\r\n"
            "To: <sip:+12025550100@carrier.test>;tag=testtag\r\n"
            f"Call-ID: {call_id}\r\nCSeq: 10 INFO\r\n"
            + "".join(f"Route: {route}\r\n" for route in reverse_routes)
            + "Content-Length: 0\r\n\r\n"
        ).encode()
        await loop.sock_sendto(uas, reverse_request, upstream_address)
        async with asyncio.timeout(5):
            while True:
                reverse_received, reverse_address = await loop.sock_recvfrom(caller, 65536)
                if reverse_received.startswith(b"INFO "):
                    break
        reverse_headers = [
            line
            for line in reverse_received.decode().split("\r\n")
            if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:"))
        ]
        await loop.sock_sendto(
            caller,
            (
                "SIP/2.0 200 OK\r\n"
                + "\r\n".join(reverse_headers)
                + "\r\nContent-Length: 0\r\n\r\n"
            ).encode(),
            reverse_address,
        )
        async with asyncio.timeout(5):
            while True:
                response, _ = await loop.sock_recvfrom(uas, 65536)
                if response.startswith(b"SIP/2.0 200") and b"CSeq: 10 INFO" in response:
                    break
        for method, sequence in (
            ("ACK", 1),
            ("INVITE", 3),
            ("ACK", 3),
            ("UPDATE", 4),
            ("INFO", 5),
            ("BYE", 6),
        ):
            dialog_request = (
                f"{method} {contact} SIP/2.0\r\n"
                f"Via: SIP/2.0/UDP {gateway}:{caller.getsockname()[1]};"
                f"branch=z9hG4bK{uuid4().hex};rport\r\n"
                "Max-Forwards: 70\r\n"
                "From: <sip:+12025550100@carrier.test>;tag=testtag\r\n"
                f"To: <sip:{number.e164}@ingress.test>;tag=uastag\r\n"
                f"Call-ID: {call_id}\r\nCSeq: {sequence} {method}\r\n"
                + "".join(f"Route: {route}\r\n" for route in route_set)
                + "Content-Length: 0\r\n\r\n"
            ).encode()
            await loop.sock_sendto(caller, dialog_request, destination)
            async with asyncio.timeout(5):
                while True:
                    in_dialog, response_address = await loop.sock_recvfrom(uas, 65536)
                    if in_dialog.startswith(method.encode() + b" "):
                        break
            if method != "ACK":
                final_headers = [
                    line
                    for line in in_dialog.decode().split("\r\n")
                    if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:"))
                ]
                await loop.sock_sendto(
                    uas,
                    (
                        "SIP/2.0 200 OK\r\n"
                        + "\r\n".join(final_headers)
                        + f"\r\nContact: <sip:uas@{gateway}:{uas.getsockname()[1]}>"
                        + "\r\nContent-Length: 0\r\n\r\n"
                    ).encode(),
                    response_address,
                )
                async with asyncio.timeout(5):
                    while True:
                        final_response, _ = await loop.sock_recvfrom(caller, 65536)
                        if (
                            final_response.startswith(b"SIP/2.0 200")
                            and f"CSeq: {sequence} {method}".encode() in final_response
                        ):
                            break
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(text("SELECT state FROM sip_route_authorizations"))
            ).scalar_one() == "ENDED"
            assert (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM sip_target_route_references WHERE target_id=:target"
                    ),
                    {"target": target.target_id},
                )
            ).scalar_one() == 0
        await placement_service.mutate(
            organization.id,
            actor,
            PlacementMutation(
                cell_id=cell,
                operation="RESUME",
                expected_generation=2,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
        second_values = json.loads(configuration.read_text())
        second_values["edge_id"] = str(second_edge)
        second_values["hmac_secret"] = second_credential.secret.hex()
        second_configuration.write_text(json.dumps(second_values))
        second_configuration.chmod(0o644)
        second_destination = await start_edge(
            second_name, second_configuration, config_directory, image=image
        )
        await loop.sock_sendto(caller, request, second_destination)
        async with asyncio.timeout(5):
            while True:
                duplicate_response, _ = await loop.sock_recvfrom(caller, 65536)
                if duplicate_response.startswith(b"SIP/2.0 503"):
                    break
        with pytest.raises(BlockingIOError):
            uas.recvfrom(65536)
        independent = request.replace(call_id.encode(), uuid4().hex.encode()).replace(
            b"branch=z9hG4bK", b"branch=z9hG4bKsecond"
        )
        await loop.sock_sendto(caller, independent, second_destination)
        async with asyncio.timeout(5):
            second_invite, _ = await loop.sock_recvfrom(
                replacement_uas if rotate_target else uas, 65536
            )
        assert second_invite.startswith(b"INVITE ")
        rejection_headers = [
            line + ";tag=declined" if line.lower().startswith("to:") else line
            for line in second_invite.decode().split("\r\n")
            if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:"))
        ]
        second_target = replacement_uas if rotate_target else uas
        second_edge_address = (
            json.loads(await docker("inspect", second_name))[0]["NetworkSettings"]["IPAddress"],
            5060,
        )
        await loop.sock_sendto(
            second_target,
            (
                "SIP/2.0 486 Busy Here\r\n"
                + "\r\n".join(rejection_headers)
                + "\r\nContent-Length: 0\r\n\r\n"
            ).encode(),
            second_edge_address,
        )
        async with asyncio.timeout(5):
            while True:
                rejected, _ = await loop.sock_recvfrom(caller, 65536)
                if rejected.startswith(b"SIP/2.0 486"):
                    break
        async with asyncio.timeout(5):
            while True:
                busy_ack, _ = await loop.sock_recvfrom(second_target, 65536)
                if busy_ack.startswith(b"ACK "):
                    break
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            bindings = (
                await tenant.session.execute(
                    text(
                        "SELECT cell_id, placement_generation, target_id, target_revision, "
                        "edge_id FROM sip_route_authorizations ORDER BY authorized_at"
                    )
                )
            ).all()
        assert len(bindings) == 2
        assert bindings[0].cell_id == bindings[1].cell_id
        assert [item.placement_generation for item in bindings] == [1, 3]
        if rotate_target:
            assert bindings[0].target_id == target.target_id
            assert bindings[1].target_id == replacement.target_id
        else:
            assert bindings[0][2:4] == bindings[1][2:4]
        assert {bindings[0].edge_id, bindings[1].edge_id} == {edge, second_edge}
        cancelled_request = request.replace(call_id.encode(), uuid4().hex.encode()).replace(
            b"branch=z9hG4bK", b"branch=z9hG4bKcancel"
        )
        await cancel_pending_invite(
            caller, replacement_uas if rotate_target else uas, destination, cancelled_request
        )
        async with tenant_database.tenant_transaction(organization.id) as tenant:
            assert (
                await tenant.session.execute(
                    text(
                        "SELECT state FROM sip_route_authorizations "
                        "ORDER BY authorized_at DESC LIMIT 1"
                    )
                )
            ).scalar_one() == "FAILED"
        other_organization = await make_organization()
        _, other_number = await provision(telephony_stack, other_organization, "+12025550998")
        placement_service = CellPlacementService(tenant_database, event_platform.publisher)
        other_cell = await placement_service.register(
            actor,
            RegisterCellRequest(
                cell_key="wire-" + uuid4().hex, idempotency_key=uuid4().hex, reason_code="TEST"
            ),
            uuid4(),
        )
        await placement_service.mutate(
            other_organization.id,
            actor,
            PlacementMutation(
                cell_id=other_cell,
                operation="ASSIGN",
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
        other_registry = TargetRegistry(
            tenant_database,
            TargetNetworkPolicy((f"{gateway}/32",), frozenset({other_cell_uas.getsockname()[1]})),
        )
        other_target = await other_registry.register(
            actor,
            RegisterTarget(
                cell_id=other_cell,
                host=gateway,
                port=other_cell_uas.getsockname()[1],
                transport="UDP",
                expected_revision=0,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
        await other_registry.transition(
            actor,
            TargetMutation(
                cell_id=other_cell,
                target_id=other_target.target_id,
                state="ACTIVE",
                expected_revision=1,
                idempotency_key=uuid4().hex,
                reason_code="TEST",
            ),
            uuid4(),
        )
        other_request = (
            request.replace(number.e164.encode(), other_number.e164.encode())
            .replace(call_id.encode(), uuid4().hex.encode())
            .replace(b"branch=z9hG4bK", b"branch=z9hG4bKother")
            .replace(
                b"Content-Length: 0",
                (
                    f"X-Organization-ID: {organization.id}\r\n"
                    f"X-Cell-ID: {cell}\r\nContent-Length: 0"
                ).encode(),
            )
        )
        await loop.sock_sendto(caller, other_request, second_destination)
        async with asyncio.timeout(5):
            other_invite, _ = await loop.sock_recvfrom(other_cell_uas, 65536)
        assert other_invite.startswith(b"INVITE ")
        assert other_number.e164.encode() in other_invite
        assert b"X-Cell-ID" not in other_invite and b"X-Organization-ID" not in other_invite
        async with tenant_database.tenant_transaction(other_organization.id) as tenant:
            assert (
                await tenant.session.execute(text("SELECT cell_id FROM sip_route_authorizations"))
            ).scalar_one() == other_cell
    finally:
        release_response.set()
        print("bounded resolver outcomes", resolver_results)
        edge_output = await docker("logs", name)
        print(edge_output)
        await docker("stop", "--time", "5", name)
        stopped = json.loads(await docker("inspect", name))[0]["State"]
        assert stopped["ExitCode"] == 0 and stopped["OOMKilled"] is False
        await docker("rm", "-f", name)
        if second_configuration.exists():
            await docker("stop", "--time", "5", second_name)
            stopped = json.loads(await docker("inspect", second_name))[0]["State"]
            assert stopped["ExitCode"] == 0 and stopped["OOMKilled"] is False
            await docker("rm", "-f", second_name)
        server.should_exit = True
        try:
            await server_task
        finally:
            uas.close()
            replacement_uas.close()
            other_cell_uas.close()
            caller.close()
            http_listener.close()
            configuration.unlink(missing_ok=True)
            second_configuration.unlink(missing_ok=True)
        assert confidential_marker not in edge_output
        assert number.e164 not in edge_output
        assert "nxs_edge_denied status=" in edge_output
