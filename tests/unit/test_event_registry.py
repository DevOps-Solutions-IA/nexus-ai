"""Event schema and version registry (NXS-EVENT-009)."""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest

from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import (
    EventContractError,
    UnknownEventTypeError,
    UnsupportedEventVersionError,
)
from nexus_ai.events.registry import EVENT_REGISTRY, EventPayload, EventRegistry


class _V1(EventPayload):
    name: str


class _V2(EventPayload):
    name: str
    tier: str


def _registry() -> EventRegistry:
    registry = EventRegistry()
    registry.register("demo.created", 1, _V1)
    registry.register("demo.created", 2, _V2)
    return registry


def _envelope(event_type: str, version: int, payload: dict[str, object]) -> EventEnvelope:
    return EventEnvelope.create(
        event_type=event_type,
        event_version=version,
        aggregate_type="demo",
        aggregate_id=uuid.uuid4().hex,
        producer="test",
        payload=payload,
    )


def test_decode_valid_payload() -> None:
    registry = _registry()
    decoded = registry.decode(_envelope("demo.created", 1, {"name": "acme"}))
    assert isinstance(decoded, _V1)
    assert decoded.name == "acme"


def test_multiple_versions_supported() -> None:
    registry = _registry()
    assert registry.supported_versions("demo.created") == (1, 2)
    decoded = registry.decode(_envelope("demo.created", 2, {"name": "a", "tier": "gold"}))
    assert isinstance(decoded, _V2)


def test_unknown_event_type_fails_closed() -> None:
    with pytest.raises(UnknownEventTypeError):
        _registry().decode(_envelope("demo.unknown", 1, {}))


def test_unsupported_version_fails_closed() -> None:
    with pytest.raises(UnsupportedEventVersionError):
        _registry().decode(_envelope("demo.created", 9, {"name": "a"}))


def test_payload_validation_failure_is_a_contract_error() -> None:
    with pytest.raises(EventContractError):
        _registry().decode(_envelope("demo.created", 1, {"name": "a", "extra": "forbidden"}))
    with pytest.raises(EventContractError):
        _registry().decode(_envelope("demo.created", 1, {}))


def test_duplicate_registration_is_rejected() -> None:
    registry = _registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register("demo.created", 1, _V1)


def test_decorator_reads_class_metadata() -> None:
    registry = EventRegistry()

    @registry.payload_model("demo.tagged")
    class _Tagged(EventPayload):
        EVENT_TYPE: ClassVar[str] = "demo.tagged"
        VERSION: ClassVar[int] = 3

        label: str

    assert registry.model_for("demo.tagged", 3) is _Tagged


def test_builtin_platform_events_are_registered() -> None:
    assert EVENT_REGISTRY.is_known_type("platform.probe.emitted")
    assert EVENT_REGISTRY.is_known_type("platform.tenant_probe.emitted")
    decoded = EVENT_REGISTRY.decode(
        _envelope("platform.probe.emitted", 1, {"nonce": "n", "note": None})
    )
    assert decoded.model_dump()["nonce"] == "n"
