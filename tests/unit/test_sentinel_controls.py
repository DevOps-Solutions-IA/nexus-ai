"""Platform authority, static targets and provider-neutral transport negatives."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid7

import pytest
from pydantic import SecretStr

from nexus_ai.agents.models.base import ModelMessage, ModelRequest, ModelRole
from nexus_ai.sentinel.config import SentinelSettings
from nexus_ai.sentinel.contracts import Approval, IncidentState, Parameters, Risk, Subject
from nexus_ai.sentinel.control import (
    SentinelControl,
    SentinelOperatorAuthority,
    SentinelPermission,
)
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sentinel.model_transport import SentinelModelTransport, SentinelProviderClient
from nexus_ai.sentinel.runbooks import (
    DiagnosticActionAdapter,
    DispatchRequest,
    HandlerBinding,
    ProvenPreEffectRejection,
    SentinelRunbookRegistry,
)
from nexus_ai.sentinel.service import SentinelStore
from tests.unit.test_sentinel_foundation import book, proposal


def http_executor(**overrides):
    return SimpleNamespace(
        _settings=SimpleNamespace(
            max_redirects=0, max_response_bytes=32768, max_request_bytes=32768
        ),
        send=AsyncMock(
            return_value=SimpleNamespace(
                final_url="https://model.example.com/v1/chat/completions",
                status_code=200,
                body=json.dumps(
                    {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
                ).encode(),
                **overrides,
            )
        ),
    )


def test_registry_consumption_is_bounded_even_for_infinite_source():
    consumed = 0

    def sources():
        nonlocal consumed
        while True:
            consumed += 1
            yield None

    with pytest.raises(SentinelDenied, match="registry_bound"):
        SentinelStore(SimpleNamespace(settings=SentinelSettings()), sources=sources())
    assert consumed == 257


@pytest.mark.anyio
async def test_platform_provider_transport_serializes_without_tenant_vault():
    executor = http_executor()
    client = SentinelProviderClient(
        SentinelModelTransport(executor, api_origin="https://model.example.com")
    )
    secret = SecretStr(uuid7().hex)
    request = ModelRequest(
        model="safe",
        messages=(ModelMessage(ModelRole.USER, "bounded"),),
        provider_api_base="https://attacker.example.com",
    )
    result = await client.generate(request, secret)
    assert result.assistant_content == "{}"
    sent = executor.send.call_args.args[0]
    assert sent.url == "https://model.example.com/v1/chat/completions"
    assert secret.get_secret_value() not in sent.body.decode()
    assert secret.get_secret_value() not in repr(sent)
    assert sent.headers["Authorization"] == "Bearer " + secret.get_secret_value()


@pytest.mark.anyio
async def test_platform_transport_failure_redacts_exception_context():
    executor = http_executor()
    secret = SecretStr(uuid7().hex)
    executor.send.side_effect = RuntimeError(secret.get_secret_value())
    client = SentinelProviderClient(
        SentinelModelTransport(executor, api_origin="https://model.example.com")
    )
    with pytest.raises(SentinelDenied) as caught:
        await client.generate(ModelRequest(model="safe", messages=()), secret)
    assert secret.get_secret_value() not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "file:///secret",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com/?key=secret",
    ],
)
def test_model_origin_injection(origin):
    with pytest.raises(SentinelDenied):
        SentinelModelTransport(http_executor(), api_origin=origin)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "override",
    [
        {"method": "GET"},
        {"url": "https://attacker.example.com"},
        {"body": b"x" * 32769},
        {"body": None},
        {"timeout_seconds": None},
        {"timeout_seconds": 121},
        {"headers": {"X-Override": "unsafe"}},
    ],
)
async def test_model_transport_request_bound(override):
    executor = http_executor()
    transport = SentinelModelTransport(executor, api_origin="https://model.example.com")
    values = dict(
        method="POST",
        url=transport.origin + "/v1/chat/completions",
        headers={},
        body=b"{}",
        timeout_seconds=1,
    )
    with pytest.raises(SentinelDenied):
        await transport.request(**(values | override))
    executor.send.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attribute,value", [("final_url", "https://other.example.com"), ("body", b"x" * 32769)]
)
async def test_model_response_bound(attribute, value):
    executor = http_executor()
    setattr(executor.send.return_value, attribute, value)
    transport = SentinelModelTransport(executor, api_origin="https://model.example.com")
    with pytest.raises(SentinelDenied):
        await transport.request(
            method="POST",
            url=transport.origin + "/v1/chat/completions",
            headers={},
            body=b"{}",
            timeout_seconds=1,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("permission", list(SentinelPermission))
async def test_organization_principal_without_platform_grant_denied(permission):
    claims = SimpleNamespace(subject=uuid7(), session_id=uuid7(), organization_id=uuid7())
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    @asynccontextmanager
    async def transaction():
        yield session

    tokens = SimpleNamespace(verify_access_token=Mock(return_value=claims))
    validator = SimpleNamespace(require_valid=AsyncMock())
    authority = SentinelOperatorAuthority(
        tokens, validator, SimpleNamespace(transaction=transaction)
    )
    with pytest.raises(SentinelDenied, match="platform_grant"):
        await authority.require("signed-token", permission)
    validator.require_valid.assert_awaited_once_with(
        user_id=claims.subject, session_id=claims.session_id, organization_id=claims.organization_id
    )
    session.scalar.return_value = claims.subject
    assert await authority.require("signed-token", permission) == claims.subject
    validator.require_valid.side_effect = SentinelDenied("revoked")
    with pytest.raises(SentinelDenied, match="revoked"):
        await authority.require("signed-token", permission)


@pytest.mark.anyio
async def test_operator_facade_cannot_spoof_approver_or_bypass_grant():
    actor = uuid7()
    authority = SimpleNamespace(require=AsyncMock(return_value=actor))
    persistence = SimpleNamespace(
        record_approval=AsyncMock(return_value=uuid7()), set_mutable_actions=AsyncMock()
    )
    executor = SimpleNamespace(claim=AsyncMock(), dispatch=AsyncMock())
    control = SentinelControl(authority, persistence, executor)
    request = proposal()
    approval = Approval(
        proposal_id=uuid7(),
        proposal_fingerprint="a" * 64,
        approver_principal=uuid7(),
        decision="APPROVED",
        reason_code="review",
        policy_revision=1,
        expires_at=request.expires_at,
    )
    await control.approve("valid", approval)
    assert persistence.record_approval.call_args.args[0].approver_principal == actor
    authority.require.side_effect = SentinelDenied("grant_missing")
    with pytest.raises(SentinelDenied):
        await control.execute("tenant", uuid7())
    with pytest.raises(SentinelDenied):
        await control.set_mutable_actions("tenant", 1, enabled=True)
    executor.claim.assert_not_awaited()
    persistence.set_mutable_actions.assert_not_awaited()


@pytest.mark.anyio
async def test_operator_facade_binds_each_authorized_operation():
    authority = SimpleNamespace(require=AsyncMock(return_value=uuid7()))
    persistence = SimpleNamespace(
        list_incidents=AsyncMock(return_value=[]),
        transition=AsyncMock(return_value=6),
        set_mutable_actions=AsyncMock(return_value=2),
    )
    executor = SimpleNamespace(
        claim=AsyncMock(return_value=object()), dispatch=AsyncMock(return_value="SUCCEEDED")
    )
    control = SentinelControl(authority, persistence, executor)
    identity = uuid7()
    assert await control.incidents("valid", limit=1) == []
    assert await control.transition("valid", identity, 5, IncidentState.RESOLVED) == 6
    persistence.transition.assert_awaited_once_with(
        identity, 5, IncidentState.RESOLVED, resolution_source="AUTHORIZED_OPERATOR"
    )
    assert await control.set_mutable_actions("valid", 1, enabled=False) == 2
    assert await control.execute("valid", identity) == "SUCCEEDED"
    assert authority.require.await_count == 5
    executor.dispatch.assert_awaited_once_with(executor.claim.return_value)


@pytest.mark.anyio
async def test_registered_readonly_adapter_exact_target():
    request = proposal()
    binding = HandlerBinding(
        "observe", 1, Risk.OBSERVE, True, ((Subject.SERVICE, request.target_id, 1),)
    )
    probe = AsyncMock()
    adapter = DiagnosticActionAdapter(binding, probe)
    registry = SentinelRunbookRegistry((adapter,))
    definition = book(handler_key="observe", handler_binding_digest=binding.fingerprint)
    assert registry.resolve(definition, request) is adapter
    dispatch = DispatchRequest(
        target_kind=Subject.SERVICE,
        target_id=request.target_id,
        target_generation=1,
        parameters=Parameters(),
        idempotency_key="stable",
    )
    assert (await adapter.execute(dispatch)).classification == "DIAGNOSED"
    with pytest.raises(ProvenPreEffectRejection):
        await adapter.execute(dispatch.model_copy(update={"target_id": uuid7()}))
    for changes in (
        {"enabled": False},
        {"handler_key": "shell"},
        {"risk": Risk.REVERSIBLE},
        {"handler_binding_digest": "a" * 64},
    ):
        with pytest.raises(SentinelDenied):
            registry.resolve(definition.model_copy(update=changes), request)
    with pytest.raises(SentinelDenied):
        registry.resolve(definition, request.model_copy(update={"target_generation": 2}))
    with pytest.raises(SentinelDenied):
        SentinelRunbookRegistry((adapter, adapter))
    assert probe.await_count == 1


@pytest.mark.parametrize(
    "risk,non_mutating,kind",
    [
        (Risk.HIGH_IMPACT, False, Subject.SERVICE),
        (Risk.DESTRUCTIVE, False, Subject.SERVICE),
        (Risk.DIAGNOSTIC, False, Subject.SERVICE),
        (Risk.REVERSIBLE, False, Subject.ORGANIZATION),
    ],
)
def test_registered_handler_cannot_downgrade_risk(risk, non_mutating, kind):
    binding = HandlerBinding("attack", 1, risk, non_mutating, ((kind, uuid7(), 1),))
    with pytest.raises(SentinelDenied):
        SentinelRunbookRegistry((SimpleNamespace(binding=binding),))
