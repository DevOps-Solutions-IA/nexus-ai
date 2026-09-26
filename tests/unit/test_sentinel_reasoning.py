import json
import os
from uuid import uuid7

import pytest

from nexus_ai.agents.models.base import ModelFinishReason, ModelResponse, ModelUsage
from nexus_ai.sentinel.config import SentinelSettings
from nexus_ai.sentinel.contracts import Facts
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sentinel.reasoning import (
    ReasoningContext,
    SentinelModelCredentialProvider,
    SentinelReasoner,
)


@pytest.fixture
def credentials(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "model-key"
    path.write_text("ephemeral-" + uuid7().hex)
    path.chmod(0o400)
    return SentinelModelCredentialProvider(tmp_path, path.name), path


def test_secret_redaction_and_permissions(credentials):
    provider, path = credentials
    secret = provider.load()
    assert secret.get_secret_value() not in repr(secret)
    for mode in (0o600, 0o440, 0o404, 0o777):
        path.chmod(mode)
        with pytest.raises(SentinelDenied, match="insecure_platform_secret"):
            provider.load()


def test_secret_symlink_and_missing_denied(credentials):
    provider, path = credentials
    path.unlink()
    with pytest.raises(SentinelDenied, match="platform_secret_unavailable"):
        provider.load()
    path.symlink_to("/etc/passwd")
    with pytest.raises(SentinelDenied, match="platform_secret_unavailable"):
        provider.load()


def test_secret_directory_and_hardlinks_denied(credentials):
    provider, path = credentials
    os.link(path, path.parent / "alias")
    with pytest.raises(SentinelDenied, match="insecure_platform_secret"):
        provider.load()
    (path.parent / "alias").unlink()
    path.parent.chmod(0o777)
    with pytest.raises(SentinelDenied, match="insecure_platform_secret_directory"):
        provider.load()


class Client:
    def __init__(self, output):
        self.output = output
        self.calls = []

    async def generate(self, request, credential):
        self.calls.append(request)
        assert not request.tools
        assert credential.get_secret_value() not in repr(request)
        if isinstance(self.output, Exception):
            raise self.output
        return ModelResponse(json.dumps(self.output), (), ModelFinishReason.STOP, ModelUsage())


def setup(credentials, output):
    provider, _ = credentials
    client = Client(output)
    return SentinelReasoner(
        SentinelSettings(reasoning_enabled=True), provider, client, provider="test", model="bounded"
    ), client


@pytest.mark.anyio
async def test_reasoner_only_advisory_bound_evidence(credentials):
    evidence = uuid7()
    reasoner, client = setup(
        credentials,
        {"category": "DEGRADED_SERVICE", "confidence": 0.7, "evidence_refs": [str(evidence)]},
    )
    context = ReasoningContext(
        incident_id=uuid7(), facts=(Facts(condition="DEGRADED"),), evidence_refs=(evidence,)
    )
    finding, suggestion = await reasoner.reason(context)
    assert finding.incident_id == context.incident_id
    assert finding.evidence_refs == (evidence,)
    assert finding.explanation == "DEGRADED SERVICE"
    assert finding.request_fingerprint
    assert suggestion is None
    assert len(client.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attack",
    ["shell", "sql", "url", "host", "credential", "target_id", "handler", "organization_id"],
)
async def test_model_payload_cannot_add_authority(credentials, attack):
    evidence = uuid7()
    reasoner, _ = setup(
        credentials,
        {
            "category": "UNKNOWN",
            "confidence": 0.1,
            "evidence_refs": [str(evidence)],
            attack: "untrusted",
        },
    )
    with pytest.raises(SentinelDenied, match="invalid_reasoner_schema"):
        await reasoner.reason(
            ReasoningContext(
                incident_id=uuid7(), facts=(Facts(condition="UNKNOWN"),), evidence_refs=(evidence,)
            )
        )


@pytest.mark.anyio
async def test_wrong_evidence_and_unknown_runbook_denied(credentials):
    evidence = uuid7()
    context = ReasoningContext(
        incident_id=uuid7(), facts=(Facts(condition="UNKNOWN"),), evidence_refs=(evidence,)
    )
    for output, code in [
        (
            {"category": "UNKNOWN", "confidence": 0.1, "evidence_refs": [str(uuid7())]},
            "unbound_reasoner_evidence",
        ),
        (
            {
                "category": "UNKNOWN",
                "confidence": 0.1,
                "evidence_refs": [str(evidence)],
                "suggestion": {"key": "shell", "revision": 1, "parameters": {}},
            },
            "unknown_reasoner_runbook",
        ),
    ]:
        reasoner, _ = setup(credentials, output)
        with pytest.raises(SentinelDenied, match=code):
            await reasoner.reason(context)


@pytest.mark.anyio
async def test_provider_exception_cannot_leak_secret(credentials):
    provider, _ = credentials
    secret = provider.load().get_secret_value()
    reasoner, _ = setup(credentials, RuntimeError(secret))
    with pytest.raises(SentinelDenied) as caught:
        await reasoner.reason(
            ReasoningContext(
                incident_id=uuid7(), facts=(Facts(condition="UNKNOWN"),), evidence_refs=(uuid7(),)
            )
        )
    assert secret not in str(caught.value)
    assert caught.value.__context__ is None
