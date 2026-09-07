"""Provisioning × P04 event platform (NXS-ORG-001 × NXS-EVENT-003/008/009).

The ``organizations.provisioned`` event travels through the transactional outbox to
real JetStream with the canonical envelope, trusted organization_id, canonical subject
and registered payload version. A NATS outage after the DB commit never corrupts
provisioning — the outbox row survives and the relay publishes on recovery.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.domain.provisioning.entities import OnboardingRequest
from nexus_ai.events.envelope import EventEnvelope

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


def _onboarding() -> OnboardingRequest:
    return OnboardingRequest(
        idempotency_key=f"evt-{uuid.uuid4().hex[:16]}",
        organization_key=f"evt-org-{uuid.uuid4().hex[:8]}",
        display_name="Event Org",
        legal_name="Event Org S.A.",
        country_code="cr",
        timezone="America/Costa_Rica",
        locale="en-US",
    )


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


class TestProvisionedEventDelivery:
    async def test_provisioned_event_reaches_jetstream_with_canonical_envelope(
        self,
        auth_client: Any,
        make_auth_user: Any,
        event_platform: Any,
        nats_messaging: Any,
        tenant_database: Any,
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )

        processed = await event_platform.relay.drain_now()
        assert processed >= 1

        js = nats_messaging.jetstream()
        psub = await js.pull_subscribe(
            event_platform.transport.tenant_subject_filter(),
            durable=f"prov-verify-{uuid.uuid4().hex[:8]}",
            stream="NXS_EVENTS",
        )
        delivered = None
        for _ in range(10):
            try:
                [msg] = await psub.fetch(1, timeout=3)
            except Exception:
                continue
            candidate = EventEnvelope.from_json(msg.data)
            if candidate.event_type == "organizations.provisioned":
                delivered = candidate
                await msg.ack()
                break
            await msg.ack()
        assert delivered is not None
        assert delivered.organization_id == result.organization_id
        assert delivered.aggregate_type == "organization"
        assert delivered.aggregate_id == str(result.organization_id)
        assert delivered.event_version == 1
        payload = delivered.payload
        assert payload["organization_id"] == str(result.organization_id)
        assert payload["organization_key"] == result.organization_key
        assert payload["created_by_user_id"] == str(user.id)
        assert payload["dashboard_revision"] == 1
        # The payload decodes through the canonical registry (fail-closed contract).
        decoded = event_platform._registry.decode(delivered)
        assert decoded.model_dump(mode="json") == payload

    async def test_subject_is_canonical_and_tenant_safe(
        self, auth_client: Any, make_auth_user: Any
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)
        result = await resources.provisioner.provision(
            _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
        )
        async with resources.database.tenant_transaction(result.organization_id) as tenant:
            subject = (
                await tenant.session.execute(
                    text(
                        "SELECT subject FROM event_outbox WHERE event_type='organizations.provisioned'"
                    )
                )
            ).scalar_one()
        # Canonical taxonomy; no tenant identifier, PII or wildcards in the subject.
        assert subject == "nxs.test.tenant.organizations.provisioned"
        assert str(result.organization_id) not in subject

    async def test_nats_outage_does_not_corrupt_provisioning(
        self,
        auth_client: Any,
        make_auth_user: Any,
        tenant_database: Any,
        nats_messaging: Any,
    ) -> None:
        resources = _resources(auth_client)
        _email, _, user = await make_auth_user()
        await resources.provisioner.grant_create_capability(user_id=user.id)

        # Take NATS down BEFORE provisioning: the outbox insert must still succeed
        # (no synchronous publish on the request path) and the org must be complete.
        await nats_messaging.disconnect()
        try:
            result = await resources.provisioner.provision(
                _onboarding(), caller_user_id=user.id, caller_has_platform_grant=True
            )
            async with resources.database.tenant_transaction(result.organization_id) as tenant:
                rows = (
                    await tenant.session.execute(
                        text(
                            "SELECT status FROM event_outbox WHERE event_type='organizations.provisioned'"
                        )
                    )
                ).all()
            assert rows and all(row[0] == "PENDING" for row in rows)
        finally:
            await nats_messaging.connect()

        # On recovery the relay publishes the surviving outbox row.
        processed = await resources.event_platform.relay.drain_now()
        assert processed >= 1
        async with resources.database.tenant_transaction(result.organization_id) as tenant:
            status = (
                await tenant.session.execute(
                    text(
                        "SELECT status FROM event_outbox WHERE event_type='organizations.provisioned'"
                    )
                )
            ).scalar_one()
        assert status == "PUBLISHED"
