"""Pinned native binaries validate configuration and reject unsafe startup."""

import asyncio
import json
import secrets
from pathlib import Path
from uuid import uuid4

import pytest

from tests.integration.sip_tls import SipPKI, edge_tls_mounts

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def run_container(*arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/docker",
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-e",
        "NXS_SIP_TOPOLOGY_KEY=" + secrets.token_hex(32),
        "--tmpfs=/run/nxs:rw,noexec,nosuid,size=16m,uid=10001,gid=10001",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async with asyncio.timeout(30):
        output, _ = await process.communicate()
    assert process.returncode is not None
    return process.returncode, output.decode()


@pytest.mark.parametrize(
    "image",
    [
        "nexus-p19-kamailio:6.1.4-development",
        "nexus-p19-kamailio:6.1.4-arm64-wolfi-development",
    ],
)
async def test_native_pinned_configuration_on_both_architectures(
    image: str, tmp_path: Path
) -> None:
    configuration = await asyncio.to_thread(Path("infrastructure/kamailio").resolve)
    code, output = await run_container(
        *edge_tls_mounts(tmp_path),
        "-v",
        f"{configuration}:/etc/nxs:ro",
        image,
        "-c",
        "-Y",
        "/run/nxs",
        "-f",
        "/etc/nxs/kamailio.cfg",
    )
    assert code == 0, output
    assert "config file ok" in output
    code, output = await run_container(image, "-V")
    assert code == 0
    assert "kamailio 6.1.4" in output
    assert ("aarch64/linux" if "arm64" in image else "x86_64/linux") in output


@pytest.mark.parametrize(
    "defect",
    [
        "missing_limits",
        "unbounded_limit",
        "wildcard_peer",
        "missing_trust",
        "missing_key",
        "readable_key",
        "writable_key",
        "invalid_ca",
        "expired_identity",
    ],
)
async def test_reference_startup_fails_closed(defect: str, tmp_path: Path) -> None:
    values = {
        "edge_id": str(uuid4()),
        "hmac_secret": secrets.token_hex(32),
        "resolver_host": "10.0.0.2",
        "resolver_port": 8081,
        "isolated_test_network": True,
        "limits": {"messages_per_second": 100, "pending_resolvers": 2, "dialogs": 100},
        "peers": [
            {
                "id": str(uuid4()),
                "network": "10.0.0.1/32",
                "direction": "INBOUND",
                "ingress_hosts": ["ingress.test"],
            }
        ],
    }
    if defect == "missing_limits":
        del values["limits"]
    elif defect == "unbounded_limit":
        values["limits"] = {"messages_per_second": 0, "pending_resolvers": 2, "dialogs": 100}
    elif defect == "wildcard_peer":
        values["peers"] = [{"id": str(uuid4()), "network": "0.0.0.0/0"}]
    elif defect == "missing_trust":
        values["isolated_test_network"] = False
    mounts = edge_tls_mounts(tmp_path)
    key = tmp_path / "sip" / "identity.key"
    if defect == "missing_key":
        key.unlink()
    elif defect == "readable_key":
        key.chmod(0o644)
    elif defect == "writable_key":
        key.chmod(0o660)
    elif defect == "invalid_ca":
        (tmp_path / "sip" / "ca.pem").write_text("invalid CA")
    elif defect == "expired_identity":
        SipPKI(tmp_path / "sip").issue("identity", "127.0.0.1", expired=True)
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(values))
    secret.chmod(0o644)
    configuration = await asyncio.to_thread(Path("infrastructure/kamailio").resolve)
    code, output = await run_container(
        *mounts,
        "-v",
        f"{configuration}:/etc/nxs:ro",
        "-v",
        f"{secret}:/run/secrets/nxs-sip-edge.json:ro",
        "nexus-p19-kamailio:6.1.4-development",
        "-DD",
        "-E",
        "-Y",
        "/run/nxs",
        "-f",
        "/etc/nxs/kamailio.cfg",
    )
    assert code != 0, output
    assert "nxs_edge_ready" not in output
    assert str(values["hmac_secret"]) not in output
