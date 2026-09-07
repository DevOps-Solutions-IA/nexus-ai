"""Canonical event envelope validation (NXS-EVENT-002, NXS-EVENT-008)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from nexus_ai.events.envelope import ENVELOPE_SCHEMA_VERSION, EventEnvelope, EventScope
from nexus_ai.events.errors import EventContractError, EventTenantScopeError


def _tenant_envelope(**overrides: object) -> EventEnvelope:
    params: dict[str, object] = {
        "event_type": "organizations.created",
        "event_version": 1,
        "aggregate_type": "organization",
        "aggregate_id": uuid.uuid4().hex,
        "producer": "nexus-ai-backend",
        "payload": {"display_name": "Acme"},
        "organization_id": uuid.uuid7(),
    }
    params.update(overrides)
    return EventEnvelope.create(**params)  # type: ignore[arg-type]


def test_create_fills_uuidv7_id_and_utc_timestamp() -> None:
    envelope = _tenant_envelope()
    assert envelope.event_id.version == 7
    assert envelope.occurred_at.tzinfo is dt.UTC
    assert envelope.schema_version == ENVELOPE_SCHEMA_VERSION
    assert envelope.scope is EventScope.TENANT
    assert envelope.correlation_id  # defaulted from the event id


def test_global_scope_when_no_organization() -> None:
    envelope = EventEnvelope.create(
        event_type="platform.probe.emitted",
        event_version=1,
        aggregate_type="platform",
        aggregate_id="x",
        producer="p",
        payload={"nonce": "n"},
    )
    assert envelope.scope is EventScope.GLOBAL
    assert envelope.organization_id is None


def test_tenant_scope_requires_organization_id() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        EventEnvelope(
            event_id=uuid.uuid7(),
            event_type="organizations.created",
            event_version=1,
            occurred_at=dt.datetime.now(dt.UTC),
            scope=EventScope.TENANT,
            organization_id=None,
            aggregate_type="organization",
            aggregate_id="a",
            correlation_id="c",
            producer="p",
        )


def test_global_scope_rejects_organization_id() -> None:
    with pytest.raises(ValueError, match="must not carry organization_id"):
        EventEnvelope(
            event_id=uuid.uuid7(),
            event_type="platform.probe.emitted",
            event_version=1,
            occurred_at=dt.datetime.now(dt.UTC),
            scope=EventScope.GLOBAL,
            organization_id=uuid.uuid7(),
            aggregate_type="platform",
            aggregate_id="a",
            correlation_id="c",
            producer="p",
        )


def test_event_id_must_be_uuid7() -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        EventEnvelope(
            event_id=uuid.uuid4(),
            event_type="organizations.created",
            event_version=1,
            occurred_at=dt.datetime.now(dt.UTC),
            scope=EventScope.GLOBAL,
            aggregate_type="organization",
            aggregate_id="a",
            correlation_id="c",
            producer="p",
        )


@pytest.mark.parametrize(
    "bad_type",
    ["Organizations.Created", "organizations", "organizations..created", "1org.created", ""],
)
def test_event_type_shape_is_enforced(bad_type: str) -> None:
    with pytest.raises(ValueError):
        _tenant_envelope(event_type=bad_type)


def test_occurred_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _tenant_envelope(occurred_at=dt.datetime(2026, 1, 1))


def test_occurred_at_is_normalised_to_utc() -> None:
    other = dt.timezone(dt.timedelta(hours=5))
    envelope = _tenant_envelope(occurred_at=dt.datetime(2026, 1, 1, 12, tzinfo=other))
    assert envelope.occurred_at.tzinfo is dt.UTC
    assert envelope.occurred_at.hour == 7


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        EventEnvelope.model_validate({**_tenant_envelope().model_dump(mode="json"), "rogue": 1})


def test_metadata_bounds() -> None:
    with pytest.raises(ValueError):
        _tenant_envelope(metadata={f"k{i}": "v" for i in range(30)})
    with pytest.raises(ValueError):
        _tenant_envelope(metadata={"k": "x" * 600})


def test_json_round_trip() -> None:
    envelope = _tenant_envelope(metadata={"source": "unit"})
    restored = EventEnvelope.from_json(envelope.to_json())
    assert restored == envelope


def test_from_json_rejects_malformed_payload() -> None:
    with pytest.raises(EventContractError):
        EventEnvelope.from_json(b'{"event_id": "not-a-uuid"}')
    with pytest.raises(EventContractError):
        EventEnvelope.from_json(b"{not json")


def test_require_tenant_and_scope_helpers() -> None:
    tenant = _tenant_envelope()
    assert tenant.require_tenant() == tenant.organization_id
    tenant.assert_matches_tenant(tenant.organization_id)
    with pytest.raises(EventTenantScopeError):
        tenant.assert_matches_tenant(uuid.uuid7())

    world = EventEnvelope.create(
        event_type="platform.probe.emitted",
        event_version=1,
        aggregate_type="platform",
        aggregate_id="a",
        producer="p",
        payload={"nonce": "n"},
    )
    with pytest.raises(EventTenantScopeError):
        world.require_tenant()


def test_subject_and_headers() -> None:
    envelope = _tenant_envelope()
    subject = envelope.subject(prefix="nxs", environment="test")
    assert subject.value == "nxs.test.tenant.organizations.created"
    headers = envelope.nats_headers()
    assert headers["Nats-Msg-Id"] == str(envelope.event_id)
    assert headers["Nxs-Organization-Id"] == str(envelope.organization_id)
    assert "event_type" in envelope.log_fields()


def test_domain_and_event_name_split() -> None:
    envelope = _tenant_envelope(event_type="organizations.profile.updated")
    assert envelope.domain == "organizations"
    assert envelope.event_name == "profile.updated"
