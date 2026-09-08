"""Tool Engine value objects, schemas, permissions, idempotency and result mapping
(NXS-TOOL-001)."""

from __future__ import annotations

import uuid

import pytest

from nexus_ai.integrations.errors import (
    IntegrationOutboundRateLimitedError,
    IntegrationTimeoutError,
    IntegrationUpstreamClientError,
)
from nexus_ai.tools.entities import (
    RegisterToolRequest,
    RiskClass,
    SideEffectClass,
    ToolBinding,
    ToolIdempotencyPolicy,
    ToolInvocation,
    ToolResult,
    ToolResultClass,
)
from nexus_ai.tools.errors import (
    ToolArgumentsInvalidError,
    ToolConfigInvalidError,
    ToolDownstreamError,
    ToolRateLimitedError,
    ToolResultInvalidError,
    ToolTimeoutError,
)
from nexus_ai.tools.idempotency import (
    ToolIdempotencyRecord,
    ToolIdempotencyStatus,
    request_fingerprint,
    result_from_record,
)
from nexus_ai.tools.permissions import IMPLIED_TOOL_PERMISSIONS, normalise_required_permissions
from nexus_ai.tools.result import map_downstream_error, tool_result_class_for
from nexus_ai.tools.schemas import (
    merge_arguments,
    validate_arguments,
    validate_output,
    validate_schema_document,
)

_OBJ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"path_params": {"type": "object"}},
}


def _binding() -> ToolBinding:
    return ToolBinding(integration_id=uuid.uuid4(), operation_key="crm.get")


# --- RegisterToolRequest coherence -----------------------------------------------


def test_side_effecting_tool_must_allow_idempotency_key() -> None:
    with pytest.raises(ValueError, match="idempotency key"):
        RegisterToolRequest(
            tool_key="t.x",
            name="x",
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_WRITE,
            idempotency_policy=ToolIdempotencyPolicy.NONE,
            input_schema=_OBJ_SCHEMA,
            binding=_binding(),
        )


def test_invocation_contract_is_minimal() -> None:
    fields = set(ToolInvocation.model_fields)
    assert fields == {"tool_key", "arguments", "idempotency_key", "correlation_id"}
    # no integration id / url / method / headers / operation key / graphql
    assert not (fields & {"integration_id", "url", "method", "headers", "operation_key", "query"})
    with pytest.raises(ValueError):
        ToolInvocation(tool_key="t.x", operation_key="crm.get")  # type: ignore[call-arg]


# --- schema validation ---------------------------------------------------------


def test_input_schema_must_be_closed_object() -> None:
    with pytest.raises(ToolConfigInvalidError, match="additionalProperties"):
        validate_schema_document({"type": "object"}, closed_object=True)
    with pytest.raises(ToolConfigInvalidError, match="object schema"):
        validate_schema_document({"type": "string"}, closed_object=True)
    validate_schema_document(_OBJ_SCHEMA, closed_object=True)


def test_schema_rejects_remote_ref_and_oversize() -> None:
    with pytest.raises(ToolConfigInvalidError, match="local in-document"):
        validate_schema_document(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"$ref": "https://evil/x"}},
            },
            closed_object=True,
        )
    with pytest.raises(ToolConfigInvalidError, match="size limit"):
        validate_schema_document(
            {"type": "object", "additionalProperties": False, "x": "z" * 70_000},
            closed_object=True,
        )


def test_schema_rejects_invalid_jsonschema() -> None:
    with pytest.raises(ToolConfigInvalidError, match="not a valid JSON Schema"):
        validate_schema_document(
            {"type": "object", "additionalProperties": False, "properties": {"a": {"type": 123}}},
            closed_object=True,
        )


def test_validate_arguments() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id"],
        "properties": {"id": {"type": "string"}},
    }
    validate_arguments({"id": "c1"}, schema)
    with pytest.raises(ToolArgumentsInvalidError):
        validate_arguments({"id": 1}, schema)
    with pytest.raises(ToolArgumentsInvalidError):
        validate_arguments({"id": "c1", "extra": "smuggled"}, schema)


def test_validate_output() -> None:
    validate_output({"id": "c1"}, {"type": "object", "required": ["id"]})
    validate_output(None, None)
    with pytest.raises(ToolResultInvalidError):
        validate_output({"nope": 1}, {"type": "object", "required": ["id"]})


def test_merge_arguments_static_wins() -> None:
    caller = {"query_params": {"status": "attacker-controlled"}, "path_params": {"id": "c1"}}
    static = {"query_params": {"status": "open", "org": "fixed"}}
    merged = merge_arguments(caller, static)
    assert merged["query_params"] == {"status": "open", "org": "fixed"}
    assert merged["path_params"] == {"id": "c1"}


# --- permissions -------------------------------------------------------------


def test_normalise_required_permissions_folds_implied() -> None:
    result = normalise_required_permissions(["customer:read"])
    assert set(IMPLIED_TOOL_PERMISSIONS) <= set(result)
    assert "customer:read" in result


def test_normalise_required_permissions_rejects_unknown() -> None:
    with pytest.raises(ToolConfigInvalidError, match="unknown permission"):
        normalise_required_permissions(["not:a:real:permission"])


# --- idempotency ----------------------------------------------------------


def test_request_fingerprint_is_deterministic_and_order_independent() -> None:
    a = request_fingerprint("t.x", 2, {"b": 1, "a": [1, 2]})
    b = request_fingerprint("t.x", 2, {"a": [1, 2], "b": 1})
    assert a == b
    assert a != request_fingerprint("t.x", 3, {"b": 1, "a": [1, 2]})
    assert a != request_fingerprint("t.y", 2, {"b": 1, "a": [1, 2]})


def test_result_from_record_marks_replayed() -> None:
    import datetime as dt

    stored = ToolResult(
        tool_key="t.x",
        tool_version=1,
        result_class=ToolResultClass.SUCCESS,
        ok=True,
        status_code=200,
        output={"id": "c1"},
    )
    record = ToolIdempotencyRecord(
        idempotency_key="k-12345678",
        request_fingerprint="fp",
        status=ToolIdempotencyStatus.COMPLETED,
        result_json=stored.model_dump(mode="json"),
        error_code=None,
        updated_at=dt.datetime.now(dt.UTC),
    )
    replayed = result_from_record(record, tool_key="t.x", tool_version=1)
    assert replayed.replayed and replayed.output == {"id": "c1"}


# --- downstream error mapping -------------------------------------------


def test_map_downstream_error() -> None:
    assert isinstance(map_downstream_error(IntegrationTimeoutError("x")), ToolTimeoutError)
    assert isinstance(
        map_downstream_error(IntegrationOutboundRateLimitedError("x")), ToolRateLimitedError
    )
    generic = map_downstream_error(IntegrationUpstreamClientError("x", upstream_status=404))
    assert isinstance(generic, ToolDownstreamError)
    assert generic.extensions["downstream_code"] == "NXS_INT_UPSTREAM_CLIENT_ERROR"
    assert generic.extensions["upstream_status"] == 404


def test_tool_result_class_for() -> None:
    assert tool_result_class_for("NXS_TOOL_ARGS_INVALID") is ToolResultClass.ARGS_INVALID
    assert tool_result_class_for("NXS_TOOL_DOWNSTREAM_ERROR") is ToolResultClass.DOWNSTREAM_ERROR
    assert tool_result_class_for("something-unknown") is ToolResultClass.EXECUTION_FAILED


def test_risk_class_ordering_is_bounded() -> None:
    assert list(RiskClass) == [
        RiskClass.LOW,
        RiskClass.MEDIUM,
        RiskClass.HIGH,
        RiskClass.CRITICAL,
    ]
