"""Pure helpers of the JetStream transport (NXS-EVENT-004)."""

from __future__ import annotations

from types import SimpleNamespace

from nexus_ai.core.config import EventsSettings
from nexus_ai.infrastructure.jetstream import JetStreamTransport, message_delivery_count


def _transport() -> JetStreamTransport:
    messaging = SimpleNamespace(is_connected=False, jetstream_enabled=None)
    return JetStreamTransport(messaging, EventsSettings(), environment="staging")  # type: ignore[arg-type]


def test_subject_helpers() -> None:
    transport = _transport()
    assert transport.main_subjects() == [
        "nxs.staging.tenant.>",
        "nxs.staging.global.>",
    ]
    assert transport.dead_letter_subject_filter() == "nxs.staging.dlq.>"
    assert (
        transport.dead_letter_subject(scope="tenant", domain="organizations")
        == "nxs.staging.dlq.tenant.organizations"
    )
    assert transport.durable_available() is False


def test_message_delivery_count_defaults_to_one_on_bad_metadata() -> None:
    good = SimpleNamespace(metadata=SimpleNamespace(num_delivered=4))
    assert message_delivery_count(good) == 4  # type: ignore[arg-type]

    class _Broken:
        @property
        def metadata(self) -> object:
            raise ValueError("no reply subject")

    assert message_delivery_count(_Broken()) == 1  # type: ignore[arg-type]
