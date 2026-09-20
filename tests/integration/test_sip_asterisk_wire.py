"""Real ARI creation-time variable reaches one PJSIP outbound INVITE."""

import asyncio
import datetime as dt
import ipaddress
import json
import secrets
import ssl
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pydantic import SecretStr

from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
from nexus_ai.telephony.providers.asterisk import AsteriskAdapter
from nexus_ai.telephony.providers.base import OutboundCallSpec, TransportResponse
from tests.integration.test_sip_wire import docker, udp_socket
from tests.unit.test_telephony_asterisk_adapter import _account

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class AriTransport:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> TransportResponse:
        response = await self.client.request(method, url, headers=headers, content=body)
        return TransportResponse(response.status_code, dict(response.headers), response.content)


def write_configuration(directory: Path, gateway: str, uas_port: int, password: str) -> Path:
    directory.chmod(0o755)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nxs-test-asterisk")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_file = directory / "tls.pem"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "tls.key").write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    configuration = {
        "asterisk.conf": """[directories]
astetcdir => /etc/asterisk
astmoddir => /usr/lib/asterisk/modules
astvarlibdir => /run/nxs
astdbdir => /run/nxs
astkeydir => /etc/asterisk
astdatadir => /var/lib/asterisk
astagidir => /var/lib/asterisk/agi-bin
astspooldir => /run/nxs
astrundir => /run/nxs
astlogdir => /run/nxs
[options]
verbose = 1
debug = 0
nofork = yes
""",
        "modules.conf": "[modules]\nautoload=yes\n",
        "http.conf": """[general]
enabled=yes
bindaddr=127.0.0.1
bindport=8088
tlsenable=yes
tlsbindaddr=0.0.0.0:8089
tlscertfile=/etc/asterisk/tls.pem
tlsprivatekey=/etc/asterisk/tls.key
""",
        "ari.conf": "[general]\nenabled=yes\n[nxs-test]\ntype=user\nread_only=no\n"
        f"password={password}\n",
        "pjsip.conf": f"""[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
[nxs-edge]
type=endpoint
transport=transport-udp
context=reject
disallow=all
allow=ulaw
aors=nxs-edge
direct_media=no
[nxs-edge]
type=aor
contact=sip:{gateway}:{uas_port}
""",
        "logger.conf": "[logfiles]\nconsole => warning,error,notice\n",
        "extensions.conf": Path("infrastructure/kamailio/asterisk-extensions.conf").read_text(),
    }
    for name, value in configuration.items():
        (directory / name).write_text(value)
    for path in directory.iterdir():
        path.chmod(0o644)
    return certificate_file


async def test_actual_ari_variable_to_pjsip_header(tmp_path: Path) -> None:
    network = json.loads(await docker("network", "inspect", "bridge"))[0]
    gateway = network["IPAM"]["Config"][0]["Gateway"]
    uas = udp_socket(gateway)
    password = secrets.token_hex(24)
    certificate = await asyncio.to_thread(
        write_configuration, tmp_path, gateway, uas.getsockname()[1], password
    )
    name = "nxs-p19-ari-" + secrets.token_hex(8)
    try:
        await docker(
            "run",
            "-d",
            "--name",
            name,
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
            f"{tmp_path}:/etc/asterisk:ro",
            "nexus-p19-asterisk:22.11.0-development",
            "-f",
            "-C",
            "/etc/asterisk/asterisk.conf",
        )
        port = int((await docker("port", name, "8089/tcp")).split(":")[-1])
        async with asyncio.timeout(30):
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/docker",
                "logs",
                "-f",
                name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                assert process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        raise RuntimeError(await docker("logs", name))
                    if b"Asterisk Ready" in line:
                        break
            finally:
                if process.returncode is None:
                    process.terminate()
                await process.wait()
        context = ssl.create_default_context(cafile=str(certificate))
        token = secrets.token_urlsafe(192)
        async with httpx.AsyncClient(verify=context, timeout=5, trust_env=False) as client:
            result = await AsteriskAdapter().create_outbound_call(
                OutboundCallSpec(
                    account=_account({"ari_base": f"https://127.0.0.1:{port}/ari"}),
                    caller_id_e164="+12025550100",
                    caller_display_name=None,
                    destination_kind="PHONE",
                    destination_value="+12025550101",
                    correlation_id=None,
                    sip_egress_permit=SecretStr(token),
                ),
                SecretMaterial(
                    CredentialType.BASIC_AUTH,
                    {
                        "username": "nxs-test",
                        "password": password,
                        "ari_user": "nxs-test",
                        "ari_password": password,
                    },
                ),
                AriTransport(client),
            )
            assert result.accepted and result.provider_call_id
            async with asyncio.timeout(5):
                message, _ = await asyncio.get_running_loop().sock_recvfrom(uas, 65536)
            assert message.startswith(b"INVITE ")
            headers = [
                line
                for line in message.decode().split("\r\n")
                if line.lower().startswith("x-nxs-egress-permit:")
            ]
            assert headers == ["X-NXS-Egress-Permit: " + token]
            assert token not in await docker("logs", name)
    finally:
        print(await docker("logs", name))
        await docker("rm", "-f", name)
        uas.close()
        for path in await asyncio.to_thread(lambda: list(tmp_path.iterdir())):
            path.unlink()
