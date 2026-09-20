"""P11 → PostgreSQL permit → real ARI/PJSIP → Kamailio → approved carrier UAS."""

import asyncio
import hashlib
import json
import secrets
import socket
import ssl
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text

from nexus_ai.cells.contracts import PlacementMutation
from nexus_ai.cells.service import CellPlacementService
from nexus_ai.sip_edge.api import RouteHandles, create_resolver_app
from nexus_ai.sip_edge.authentication import EdgeAuthenticator
from nexus_ai.sip_edge.egress import EgressPermits
from nexus_ai.sip_edge.inbound import InboundRoutes
from nexus_ai.sip_edge.locator import DidLocator
from nexus_ai.sip_edge.peer_registry import PeerRegistry
from nexus_ai.sip_edge.peers import PeerPolicy, PeerProfile
from nexus_ai.sip_edge.security import EdgeCredential
from nexus_ai.sip_edge.upstreams import RegisterUpstream, UpstreamRegistry
from nexus_ai.telephony.entities import (
    CreateAccountRequest,
    CreateCallRequest,
    RegisterPhoneNumberRequest,
)
from nexus_ai.telephony.service import TelephonyService
from tests.integration.sip_tls import resolver_certificate
from tests.integration.test_sip_asterisk_wire import AriTransport, write_configuration
from tests.integration.test_sip_did_locator import discovery_database as discovery_database
from tests.integration.test_sip_target_constraints import target_control as target_control
from tests.integration.test_sip_wire import docker, isolated_sender, start_edge, udp_socket

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class ObservedAriTransport(AriTransport):
    def __init__(self, client: httpx.AsyncClient) -> None:
        super().__init__(client)
        self.permits: list[SecretStr] = []

    async def request(self, **arguments: Any) -> Any:
        payload = json.loads(arguments.get("body") or b"{}")
        token = payload.get("variables", {}).get("__NXS_SIP_EGRESS_PERMIT")
        if token:
            self.permits.append(SecretStr(token))
        return await super().request(**arguments)


@pytest.mark.parametrize("lose_consume_response", [False, True])
async def test_real_p11_ari_edge_permit_is_consumed_and_stripped(
    target_control: Any,
    tenant_database: Any,
    event_platform: Any,
    make_organization: Any,
    telephony_stack: Any,
    discovery_database: Any,
    tmp_path: Path,
    lose_consume_response: bool,
) -> None:
    cell, actor = target_control
    organization = await make_organization()
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
    subnet = f"10.252.{secrets.randbelow(250) + 1}"
    suffix = uuid4().hex
    network, edge_name, asterisk_name = (
        "nxs-egress-net-" + suffix,
        "nxs-egress-edge-" + suffix,
        "nxs-egress-ari-" + suffix,
    )
    await docker("network", "create", "--subnet", subnet + ".0/24", network)
    uas = udp_socket(gateway)
    http_listener = socket.socket()
    http_listener.bind((gateway, 0))
    http_listener.listen(32)
    edge, peer, upstream = uuid4(), uuid4(), uuid4()
    profile = PeerProfile(
        peer_id=peer,
        direction="OUTBOUND",
        edge_ids=(edge,),
        networks=(subnet + ".3/32",),
        transport="UDP",
        isolated_network=True,
        cell_id=cell,
    )
    await PeerRegistry(tenant_database).register(actor, profile, expected_revision=0)
    credential = EdgeCredential(edge, secrets.token_bytes(32))
    policy = PeerPolicy((profile,))
    permits = EgressPermits(tenant_database, Fernet.generate_key(), policy)
    app = create_resolver_app(
        EdgeAuthenticator(tenant_database, (credential,)),
        policy,
        InboundRoutes(tenant_database, DidLocator(discovery_database)),
        RouteHandles(Fernet.generate_key()),
        permits,
    )
    result_committed = asyncio.Event()
    consume_committed, release_response = asyncio.Event(), asyncio.Event()

    @app.middleware("http")
    async def observe_result(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path == "/internal/sip/result" and response.status_code == 200:
            result_committed.set()
        if (
            lose_consume_response
            and request.url.path == "/internal/sip/egress"
            and response.status_code == 200
        ):
            consume_committed.set()
            await release_response.wait()
        return response

    resolver_cert, resolver_key = await asyncio.to_thread(resolver_certificate, tmp_path, gateway)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            lifespan="off",
            ws="none",
            interface="asgi3",
            ssl_certfile=str(resolver_cert),
            ssl_keyfile=str(resolver_key),
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[http_listener]))
    edge_config = tmp_path / "edge.json"
    edge_config.write_text(
        json.dumps(
            {
                "edge_id": str(edge),
                "hmac_secret": credential.secret.hex(),
                "resolver_host": gateway,
                "resolver_port": http_listener.getsockname()[1],
                "isolated_test_network": True,
                "edge_hosts": [subnet + ".2"],
                "limits": {"messages_per_second": 100, "pending_resolvers": 2, "dialogs": 100},
                "peers": [
                    {
                        "id": str(peer),
                        "network": subnet + ".3/32",
                        "direction": "OUTBOUND",
                        "ingress_hosts": [],
                    }
                ],
            }
        )
    )
    edge_config.chmod(0o644)
    asterisk_config = tmp_path / "asterisk"
    asterisk_config.mkdir()
    password = secrets.token_hex(24)
    certificate = await asyncio.to_thread(
        write_configuration, asterisk_config, subnet + ".2", 5060, password
    )
    started: list[str] = []
    try:
        started.append(edge_name)
        await start_edge(
            edge_name,
            edge_config,
            await asyncio.to_thread(Path("infrastructure/kamailio").resolve),
            network=network,
            address=subnet + ".2",
        )
        started.append(asterisk_name)
        await docker(
            "run",
            "-d",
            "--name",
            asterisk_name,
            "--network",
            network,
            "--ip",
            subnet + ".3",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/run/nxs:rw,noexec,nosuid,size=32m,uid=10001,gid=10001",
            "-p",
            "127.0.0.1::8089",
            "-v",
            f"{asterisk_config}:/etc/asterisk:ro",
            "nexus-p19-asterisk:22.11.0-development",
            "-f",
            "-C",
            "/etc/asterisk/asterisk.conf",
        )
        async with asyncio.timeout(30):
            logs = await asyncio.create_subprocess_exec(
                "/usr/bin/docker",
                "logs",
                "-f",
                asterisk_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                assert logs.stdout is not None
                while True:
                    line = await logs.stdout.readline()
                    if not line:
                        raise RuntimeError(await docker("logs", asterisk_name))
                    if b"Asterisk Ready" in line:
                        break
            finally:
                if logs.returncode is None:
                    logs.terminate()
                await logs.wait()
        port = int((await docker("port", asterisk_name, "8089/tcp")).split(":")[-1])
        account = await telephony_stack.service.create_account(
            organization.id,
            CreateAccountRequest(
                provider="asterisk",
                slug="ari-" + suffix,
                external_account_id=suffix,
                configuration={"ari_base": f"https://127.0.0.1:{port}/ari"},
            ),
        )
        number = await telephony_stack.service.register_number(
            organization.id,
            RegisterPhoneNumberRequest(
                account_id=account.id, e164="+12025550560", inbound_enabled=True
            ),
        )
        await telephony_stack.service.set_number_verified(organization.id, number.id, verified=True)
        await telephony_stack.service.store_account_credential(
            organization.id, account.id, {"ari_user": "nxs-test", "ari_password": password}
        )
        registry = UpstreamRegistry(tenant_database)
        await registry.register(
            actor,
            RegisterUpstream(
                id=upstream,
                revision=1,
                host=gateway,
                port=uas.getsockname()[1],
                transport="UDP",
                cell_id=cell,
                asterisk_peer_id=peer,
            ),
        )
        await registry.bind_account(
            actor, organization.id, account.id, upstream, 1, expected_revision=0
        )
        context = ssl.create_default_context(cafile=str(certificate))
        async with httpx.AsyncClient(verify=context, timeout=5, trust_env=False) as client:
            transport = ObservedAriTransport(client)
            service = TelephonyService(
                telephony_stack.settings,
                tenant_database,
                event_platform.publisher,
                telephony_stack.vault,
                transport,
                sip_permits=permits,
            )
            request = CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=number.id,
                destination="+12025550561",
                idempotency_key=uuid4().hex,
                metadata={"X-NXS-Egress-Permit": "tenant-forgery"},
            )
            call = await service.create_call(organization.id, None, request)
            if lose_consume_response:
                await asyncio.wait_for(consume_committed.wait(), timeout=5)
                with pytest.raises(TimeoutError):
                    async with asyncio.timeout(2):
                        await asyncio.get_running_loop().sock_recvfrom(uas, 65536)
                async with tenant_database.tenant_transaction(organization.id) as tenant:
                    assert (
                        await tenant.session.execute(
                            text("SELECT state FROM sip_egress_permits WHERE call_id=:call"),
                            {"call": call.id},
                        )
                    ).scalar_one() == "CONSUMED"
                    assert (
                        await tenant.session.execute(text("SELECT count(*) FROM sip_egress_routes"))
                    ).scalar_one() == 1
                assert (await service.create_call(organization.id, None, request)).id == call.id
                assert len(transport.permits) == 1
                release_response.set()
                return
            async with asyncio.timeout(10):
                received, edge_address = await asyncio.get_running_loop().sock_recvfrom(uas, 65536)
            assert received.startswith(f"INVITE sip:+12025550561@{gateway}:".encode())
            assert b"X-NXS-" not in received and b"tenant-forgery" not in received
            assert (await service.create_call(organization.id, None, request)).id == call.id
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                permit = (
                    await tenant.session.execute(
                        text(
                            "SELECT id, state, token_digest FROM sip_egress_permits "
                            "WHERE call_id=:call"
                        ),
                        {"call": call.id},
                    )
                ).one()
                assert permit.state == "CONSUMED"
                assert (
                    await tenant.session.execute(text("SELECT count(*) FROM sip_egress_routes"))
                ).scalar_one() == 1
            assert hashlib.sha256(b"tenant-forgery").hexdigest() != permit.token_digest
            assert "tenant-forgery" not in await docker("logs", asterisk_name)
            assert len(transport.permits) == 1
            token = transport.permits[0]
            assert (
                hashlib.sha256(token.get_secret_value().encode()).hexdigest() == permit.token_digest
            )
            assert token.get_secret_value() not in await docker("logs", asterisk_name)
            response_headers = [
                line + ";tag=carrier-declined" if line.lower().startswith("to:") else line
                for line in received.decode().split("\r\n")
                if line.lower().startswith(("via:", "from:", "to:", "call-id:", "cseq:"))
            ]
            loop = asyncio.get_running_loop()
            await loop.sock_sendto(
                uas,
                (
                    "SIP/2.0 486 Busy Here\r\n"
                    + "\r\n".join(response_headers)
                    + "\r\nContent-Length: 0\r\n\r\n"
                ).encode(),
                edge_address,
            )
            async with asyncio.timeout(5):
                while True:
                    acknowledgment, _ = await loop.sock_recvfrom(uas, 65536)
                    if acknowledgment.startswith(b"ACK "):
                        break
            await asyncio.wait_for(result_committed.wait(), timeout=3)
            async with tenant_database.tenant_transaction(organization.id) as tenant:
                assert (
                    await tenant.session.execute(
                        text("SELECT state FROM sip_egress_permits WHERE call_id=:call"),
                        {"call": call.id},
                    )
                ).scalar_one() == "ENDED"
            await docker("stop", "--time", "2", asterisk_name)
            await docker("network", "disconnect", network, asterisk_name)
            for attack in ("missing", "forged", "consumed", "wrong_destination", "wrong_upstream"):
                destination = "+12025550000" if attack == "wrong_destination" else "+12025550561"
                host = "198.51.100.10" if attack == "wrong_upstream" else subnet + ".2"
                header = ""
                if attack != "missing":
                    material = "a" * 256 if attack == "forged" else token.get_secret_value()
                    header = f"X-NXS-Egress-Permit: {material}\r\n"
                packet = (
                    f"INVITE sip:{destination}@{host}:5060 SIP/2.0\r\n"
                    f"Via: SIP/2.0/UDP {subnet}.3:5060;branch=z9hG4bK{uuid4().hex};rport\r\n"
                    "Max-Forwards: 70\r\nFrom: <sip:+12025550560@asterisk.test>;tag=attack\r\n"
                    f"To: <sip:{destination}@{host}>\r\n"
                    f"Call-ID: {uuid4().hex}\r\nCSeq: 1 INVITE\r\n"
                    f"Contact: <sip:asterisk@{subnet}.3:5060>\r\n{header}Content-Length: 0\r\n\r\n"
                ).encode()
                rejected = await isolated_sender(
                    packet, subnet + ".2", network=network, address=subnet + ".3"
                )
                assert rejected.startswith(("SIP/2.0 403", "SIP/2.0 503"))
                with pytest.raises(BlockingIOError):
                    uas.recvfrom(65536)
            assert token.get_secret_value() not in await docker("logs", edge_name)
    finally:
        release_response.set()
        for name in reversed(started):
            print(await docker("logs", name))
            await docker("rm", "-f", name)
        server.should_exit = True
        await server_task
        http_listener.close()
        uas.close()
        await docker("network", "rm", network)
        edge_config.unlink(missing_ok=True)
        for path in asterisk_config.iterdir():
            path.unlink()
