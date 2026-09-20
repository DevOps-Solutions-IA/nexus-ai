"""Disposable resolver TLS identity; private key never mounted into the edge."""

import datetime as dt
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def resolver_certificate(directory: Path, address: str) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nxs-disposable-resolver")])
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
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    certificate_path, key_path = directory / "resolver-ca.pem", directory / "resolver.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.chmod(0o644)
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return certificate_path, key_path


class SipPKI:
    """Per-test CA and explicit peer identities; no persisted repository secrets."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o755)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "disposable-sip-ca")])
        now = dt.datetime.now(dt.UTC)
        self.ca = (
            x509.CertificateBuilder()
            .subject_name(self.subject)
            .issuer_name(self.subject)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(hours=4))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(self.key, hashes.SHA256())
        )
        (directory / "ca.pem").write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        (directory / "ca.pem").chmod(0o644)

    def issue(self, name: str, address: str, *, expired: bool = False) -> tuple[Path, Path, str]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = dt.datetime.now(dt.UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(self.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=2))
            .not_valid_after(now - dt.timedelta(days=1) if expired else now + dt.timedelta(hours=4))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
            .sign(self.key, hashes.SHA256())
        )
        certificate_path, key_path = self.directory / f"{name}.pem", self.directory / f"{name}.key"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        certificate_path.chmod(0o644)
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o640)
        return certificate_path, key_path, certificate.fingerprint(hashes.SHA256()).hex()


def edge_tls_mounts(directory: Path) -> tuple[str, ...]:
    material = directory / "sip"
    if not (material / "identity.pem").exists():
        SipPKI(material).issue("identity", "127.0.0.1")
    return "--group-add", str(os.getgid()), "-v", f"{material}:/run/secrets/sip:ro"
