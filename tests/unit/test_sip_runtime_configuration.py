"""Internal configuration is explicit, bounded and never tenant supplied."""

import json
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from nexus_ai.core.config import Settings, SipEdgeSettings
from nexus_ai.core.errors import ConfigurationError
from nexus_ai.sip_edge.runtime import ResolverSecrets, load_secrets
from nexus_ai.telephony.errors import TelephonyConfigInvalidError
from nexus_ai.telephony.service import TelephonyService


def configuration() -> dict:
    edge = str(uuid4())
    return {
        "edges": [{"edge_id": edge, "hmac_secret": secrets.token_hex(32)}],
        "peers": [
            {
                "peer_id": str(uuid4()),
                "direction": "INBOUND",
                "edge_ids": [edge],
                "networks": ["10.0.0.0/8"],
                "transport": "UDP",
                "isolated_network": True,
                "ingress_hosts": ["ingress.test"],
            }
        ],
        "permit_key": Fernet.generate_key().decode(),
        "handle_key": Fernet.generate_key().decode(),
    }


def settings(path: Path) -> Settings:
    return Settings(
        sip_edge={
            "enabled": True,
            "secret_file": str(path),
            "locator_dsn": "postgresql+asyncpg://nexus_sip_locator@localhost/test",
        }
    )


def test_explicit_secret_configuration_and_missing_issuer_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SipEdgeSettings(enabled=True)
    with pytest.raises(ConfigurationError):
        load_secrets(Settings())
    path = tmp_path / "sip.json"
    value = configuration()
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    parsed = load_secrets(settings(path))
    for secret in (value["permit_key"], value["handle_key"], value["edges"][0]["hmac_secret"]):
        assert secret not in repr(parsed)
    with pytest.raises(TelephonyConfigInvalidError):
        TelephonyService(settings(path), None, None, None, None)
    path.chmod(0o666)
    with pytest.raises(ConfigurationError):
        load_secrets(settings(path))
    path.chmod(0o644)
    with pytest.raises(ConfigurationError):
        load_secrets(settings(path))
    path.chmod(0o600)
    path.write_text("x" * 65537)
    with pytest.raises(ConfigurationError):
        load_secrets(settings(path))
    path.unlink()
    with pytest.raises(ConfigurationError):
        load_secrets(settings(path))


@pytest.mark.parametrize(
    "variant", ["duplicate", "short_hmac", "invalid_hmac", "permit", "handle", "wildcard", "extra"]
)
def test_invalid_runtime_secrets_are_rejected(variant: str) -> None:
    value = configuration()
    if variant == "duplicate":
        value["edges"].append(value["edges"][0])
    elif variant == "short_hmac":
        value["edges"][0]["hmac_secret"] = "ab"
    elif variant == "invalid_hmac":
        value["edges"][0]["hmac_secret"] = "invalid"
    elif variant in {"permit", "handle"}:
        value[variant + "_key"] = "invalid"
    elif variant == "wildcard":
        value["peers"][0]["networks"] = ["0.0.0.0/0"]
    else:
        value["organization_id"] = str(uuid4())
    with pytest.raises(ValueError):
        ResolverSecrets.model_validate(value)
