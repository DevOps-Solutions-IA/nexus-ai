"""Telephony stable contracts — states, directions, schemas, taxonomy, events, OpenAPI,
RBAC scopes (NXS-P11). NXS-P12 consumes these without redesigning P11."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.domain.auth.rbac import PermissionKey
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.telephony.entities import (
    CallDirection,
    CallState,
    CallView,
    CreateCallRequest,
    DtmfDigit,
    MediaState,
    TelephonyProvider,
)
from nexus_ai.telephony.errors import TELEPHONY_ERRORS

pytestmark = pytest.mark.anyio

_FORBIDDEN_CALL_FIELDS = {
    "from",
    "from_number",
    "sip_headers",
    "dialplan",
    "ari_url",
    "ami_action",
    "provider_secret",
    "command",
    "headers",
    "route",
}


def test_call_states_and_directions_are_stable() -> None:
    assert [s.value for s in CallState] == [
        "CREATED",
        "RINGING",
        "EARLY_MEDIA",
        "ANSWERED",
        "BRIDGED",
        "ENDING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "BUSY",
        "NO_ANSWER",
    ]
    assert [d.value for d in CallDirection] == ["INBOUND", "OUTBOUND"]
    assert [s.value for s in MediaState] == ["PENDING", "ACTIVE", "STOPPED"]
    assert {d.value for d in DtmfDigit} == set("0123456789*#ABCD")
    assert [p.value for p in TelephonyProvider] == ["asterisk", "fake"]


def test_create_call_request_has_no_raw_transport_surface() -> None:
    fields = set(CreateCallRequest.model_fields)
    assert fields == {
        "provider_account_id",
        "from_number_id",
        "destination",
        "correlation_id",
        "idempotency_key",
        "metadata",
    }
    assert not (fields & _FORBIDDEN_CALL_FIELDS)


def test_call_view_carries_no_credential_or_raw_sdp() -> None:
    fields = set(CallView.model_fields)
    for banned in ("sdp", "sip_password", "ari_password", "authorization", "credential"):
        assert banned not in fields


def test_error_taxonomy_is_stable_unique_and_rfc9457() -> None:
    codes = [e.code for e in TELEPHONY_ERRORS]
    assert len(codes) == len(set(codes))
    assert set(codes) == {
        "NXS_TELEPHONY_CALL_NOT_FOUND",
        "NXS_TELEPHONY_ACCOUNT_NOT_FOUND",
        "NXS_TELEPHONY_NUMBER_NOT_FOUND",
        "NXS_TELEPHONY_INVALID_DESTINATION",
        "NXS_TELEPHONY_INVALID_STATE",
        "NXS_TELEPHONY_CONFIG_INVALID",
        "NXS_TELEPHONY_PROVIDER_ERROR",
        "NXS_TELEPHONY_PROVIDER_TIMEOUT",
        "NXS_TELEPHONY_WEBHOOK_INVALID",
        "NXS_TELEPHONY_WEBHOOK_REPLAY",
        "NXS_TELEPHONY_IDEMPOTENCY_CONFLICT",
        "NXS_TELEPHONY_NOT_AUTHORIZED",
        "NXS_TELEPHONY_DTMF_INVALID",
    }
    for error in TELEPHONY_ERRORS:
        assert issubclass(error, NxsError)
        assert 400 <= error.status < 600


def test_rbac_scopes_exist() -> None:
    assert PermissionKey.TELEPHONY_READ.value == "telephony:read"
    assert PermissionKey.TELEPHONY_CALL.value == "telephony:call"
    assert PermissionKey.TELEPHONY_HANGUP.value == "telephony:hangup"
    assert PermissionKey.TELEPHONY_CONFIGURE.value == "telephony:configure"


def test_event_payloads_registered_and_strict() -> None:
    from pydantic import ValidationError

    for event_type in (
        "telephony.call.created",
        "telephony.call.ringing",
        "telephony.call.answered",
        "telephony.call.bridged",
        "telephony.call.ending",
        "telephony.call.completed",
        "telephony.call.failed",
        "telephony.call.busy",
        "telephony.call.no_answer",
        "telephony.call.cancelled",
        "telephony.dtmf.received",
        "telephony.media.started",
        "telephony.media.stopped",
    ):
        assert EVENT_REGISTRY.is_known_type(event_type)
        model = EVENT_REGISTRY.model_for(event_type, 1)
        with pytest.raises(ValidationError):
            model.model_validate({"unexpected": 1})


async def test_openapi_surface_is_governed_only(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    assert "/api/v1/telephony/calls" in paths
    assert "/api/v1/telephony/calls/{call_id}/hangup" in paths
    assert "/api/v1/telephony/calls/{call_id}/dtmf" in paths
    assert "/api/v1/telephony/numbers" in paths
    assert "/api/v1/webhooks/telephony/{provider}/{token}" in paths
    for path in paths:
        assert not any(
            token in path
            for token in ("/telephony/ari", "/telephony/ami", "/telephony/dialplan", "/sip/raw")
        ), path

    create = schema["paths"]["/api/v1/telephony/calls"]["post"]
    ref = create["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    props = set(schema["components"]["schemas"][ref.split("/")[-1]]["properties"])
    assert props == {
        "provider_account_id",
        "from_number_id",
        "destination",
        "correlation_id",
        "idempotency_key",
        "metadata",
    }
    assert "from" not in props


def test_p12_and_later_phases_remain_planned() -> None:
    import json
    from pathlib import Path

    registry = json.loads(Path(".nxs/phase-registry.json").read_text())
    phases = {p["id"]: p for p in registry["phases"]}
    # Workflow is the current phase; scheduler and campaigns remain future scope.
    assert phases["NXS-P14"]["status"] in {"BUILDING", "VALIDATING", "READY"}
    for later in ("NXS-P15", "NXS-P16"):
        assert phases[later]["status"] == "PLANNED", later
    # P11 does NOT prematurely implement ElevenLabs / AI runtime
    import inspect

    import nexus_ai.telephony.service as telephony_service

    source = inspect.getsource(telephony_service).lower()
    assert "elevenlabs" not in source
    assert "ai_agent" not in source and "agent_runtime" not in source
