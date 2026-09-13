"""Messaging channels stable contracts — schemas, taxonomy, API surface (NXS-P09)."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.messaging.entities import (
    MessageChannel,
    MessageContent,
    MessageContentType,
    MessageDirection,
    MessageStatus,
    MessageView,
    SendMessageRequest,
)
from nexus_ai.messaging.errors import MESSAGING_ERRORS

pytestmark = pytest.mark.anyio

_FORBIDDEN_SEND_FIELDS = {
    "provider",
    "provider_message_id",
    "url",
    "method",
    "headers",
    "organization_id",
    "sender",
    "raw",
    "smtp",
    "callback_url",
}


def test_channel_direction_and_status_enums_are_stable() -> None:
    assert [c.value for c in MessageChannel] == ["WHATSAPP", "EMAIL", "SMS"]
    assert [d.value for d in MessageDirection] == ["INBOUND", "OUTBOUND"]
    assert [s.value for s in MessageStatus] == [
        "RECEIVED",
        "QUEUED",
        "SENDING",
        "SENT",
        "DELIVERED",
        "READ",
        "FAILED",
    ]


def test_send_request_has_no_execution_primitive() -> None:
    fields = set(SendMessageRequest.model_fields)
    assert fields == {
        "account_id",
        "conversation_id",
        "to",
        "content",
        "subject",
        "reply_to_message_id",
        "idempotency_key",
        "correlation_id",
    }
    assert not (fields & _FORBIDDEN_SEND_FIELDS)


def test_message_view_round_trips() -> None:
    import datetime as dt
    import uuid

    view = MessageView(
        id=uuid.uuid7(),
        organization_id=uuid.uuid7(),
        conversation_id=uuid.uuid7(),
        customer_id=None,
        channel=MessageChannel.SMS,
        direction=MessageDirection.OUTBOUND,
        status=MessageStatus.SENT,
        provider="generic_http",
        provider_account_id=uuid.uuid7(),
        provider_message_id="sm-1",
        sender={"kind": "PHONE", "value": "+14155550100"},  # type: ignore[arg-type]
        recipients=({"kind": "PHONE", "value": "+14155550142"},),  # type: ignore[arg-type]
        content=MessageContent(content_type=MessageContentType.TEXT, text="hi"),
        email=None,
        sms=None,
        reply_to_message_id=None,
        correlation_id=None,
        idempotency_key=None,
        error_code=None,
        provider_timestamp=None,
        sent_at=dt.datetime.now(dt.UTC),
        delivered_at=None,
        read_at=None,
        failed_at=None,
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )
    assert MessageView.model_validate(view.model_dump(mode="json")) == view


def test_error_taxonomy_is_stable_unique_and_rfc9457() -> None:
    codes = [e.code for e in MESSAGING_ERRORS]
    assert len(codes) == len(set(codes))
    for error in MESSAGING_ERRORS:
        assert error.code.startswith("NXS_MSG_")
        assert issubclass(error, NxsError)
        assert error.title and error.title != NxsError.title


def test_event_payloads_registered_and_strict() -> None:
    from pydantic import ValidationError

    for event_type in (
        "messaging.message.received",
        "messaging.message.queued",
        "messaging.message.sent",
        "messaging.message.delivered",
        "messaging.message.read",
        "messaging.message.failed",
    ):
        assert EVENT_REGISTRY.is_known_type(event_type)
        model = EVENT_REGISTRY.model_for(event_type, 1)
        with pytest.raises(ValidationError):
            model.model_validate({"unexpected": 1})


async def test_api_has_no_arbitrary_provider_surface(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    banned = ("run-anything", "/http/request", "/smtp", "/proxy", "raw-provider", "/forward")
    for path in paths:
        assert not any(token in path for token in banned), path
    assert "/api/v1/messaging/accounts" in paths
    assert "/api/v1/messaging/messages" in paths
    assert "/api/v1/webhooks/messaging/{provider}/{token}" in paths
    # the send body is exactly the governed contract
    send = schema["paths"]["/api/v1/messaging/messages"]["post"]
    ref = send["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    props = set(schema["components"]["schemas"][ref.split("/")[-1]]["properties"])
    assert props == {
        "account_id",
        "conversation_id",
        "to",
        "content",
        "subject",
        "reply_to_message_id",
        "idempotency_key",
        "correlation_id",
    }


def test_p10_and_later_phases_remain_planned() -> None:
    import json
    from pathlib import Path

    registry = json.loads(Path(".nxs/phase-registry.json").read_text())
    phases = {p["id"]: p for p in registry["phases"]}
    # Workflow is the current phase; campaigns remain future scope.
    assert phases["NXS-P14"]["status"] in {"BUILDING", "VALIDATING", "READY"}
    for later in ("NXS-P16",):
        assert phases[later]["status"] == "PLANNED", later
    # P09 provides the generic SMS delivery mechanism but no OTP semantics
    import nexus_ai.messaging.providers.sms as sms_module

    source = Path(sms_module.__file__).read_text().lower()
    assert "otp" in source  # only the explicit "contains NO OTP" disclaimer
    assert "one_time_password" not in source
    assert "verify_code" not in source
