"""Deterministic event contracts and the absence of a public event API (section 23, 28)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nexus_ai.events.envelope import ENVELOPE_SCHEMA_VERSION, EventEnvelope
from nexus_ai.events.registry import EVENT_REGISTRY

ROOT = Path(__file__).parents[2]

pytestmark = [pytest.mark.anyio]


def test_envelope_schema_is_stable() -> None:
    fields = set(EventEnvelope.model_fields)
    assert fields == {
        "schema_version",
        "event_id",
        "event_type",
        "event_version",
        "occurred_at",
        "scope",
        "organization_id",
        "aggregate_type",
        "aggregate_id",
        "correlation_id",
        "causation_id",
        "trace_id",
        "producer",
        "payload",
        "metadata",
    }
    assert ENVELOPE_SCHEMA_VERSION == "1.0"


def test_platform_event_contracts_are_registered_and_versioned() -> None:
    assert EVENT_REGISTRY.supported_versions("platform.probe.emitted") == (1,)
    assert EVENT_REGISTRY.supported_versions("platform.tenant_probe.emitted") == (1,)


def test_delivery_semantics_are_documented_precisely() -> None:
    doc = (ROOT / "docs/engineering/events.md").read_text(encoding="utf-8").lower()
    assert "at-least-once" in doc
    assert "exactly-once" in doc
    assert "idempotent" in doc
    assert "transactional outbox" in doc


async def test_no_public_event_endpoints(app_client) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    paths = schema.get("paths", {})
    assert not any("/events" in path for path in paths)
    assert "/api/v1/events/publish" not in paths


def test_adrs_exist() -> None:
    for name in (
        "0047-event-delivery-semantics.md",
        "0048-transactional-outbox.md",
        "0049-event-envelope-and-subjects.md",
    ):
        assert (ROOT / "docs/adr" / name).exists(), name
