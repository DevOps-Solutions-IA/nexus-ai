"""Event schema and version registry (NXS-EVENT-009).

A decoder registry of typed Pydantic payload models keyed by ``(event_type, version)``.
No consumer ever treats an unknown payload as a trusted structure: it decodes through
this registry, which fails closed for unknown types and unsupported versions.

Backward-compatible evolution rules (documented in ``docs/engineering/events.md``):

* Additive optional fields are a compatible change WITHIN a payload version.
* A required field, a removed field or a changed meaning requires a NEW payload version.
* Multiple payload versions of one ``event_type`` may be registered at once; consumers
  declare the versions they understand.
* Payload models are ``extra="forbid"`` — an unexpected field at a trusted boundary is a
  contract error, never silently accepted.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import (
    EventContractError,
    UnknownEventTypeError,
    UnsupportedEventVersionError,
)


class EventPayload(BaseModel):
    """Base for every typed event payload. Strict by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class EventRegistry:
    def __init__(self) -> None:
        self._models: dict[tuple[str, int], type[EventPayload]] = {}

    def register(
        self, event_type: str, version: int, model: type[EventPayload]
    ) -> type[EventPayload]:
        key = (event_type, version)
        if key in self._models:
            raise ValueError(f"payload model already registered for {event_type} v{version}")
        self._models[key] = model
        return model

    def payload_model(self, event_type: str) -> Any:
        """Decorator: ``@registry.payload_model('x.y')`` reads ``EVENT_TYPE``/``VERSION``."""

        def _wrap(model: type[EventPayload]) -> type[EventPayload]:
            declared_type = getattr(model, "EVENT_TYPE", event_type)
            version = getattr(model, "VERSION", 1)
            return self.register(declared_type, int(version), model)

        return _wrap

    def is_known_type(self, event_type: str) -> bool:
        return any(known == event_type for known, _ in self._models)

    def supported_versions(self, event_type: str) -> tuple[int, ...]:
        return tuple(sorted(v for t, v in self._models if t == event_type))

    def model_for(self, event_type: str, version: int) -> type[EventPayload]:
        try:
            return self._models[(event_type, version)]
        except KeyError:
            if not self.is_known_type(event_type):
                raise UnknownEventTypeError(
                    "no payload model is registered for this event type",
                    extensions={"event_type": event_type},
                ) from None
            raise UnsupportedEventVersionError(
                "this payload version is not supported",
                extensions={
                    "event_type": event_type,
                    "requested_version": version,
                    "supported_versions": list(self.supported_versions(event_type)),
                },
            ) from None

    def decode(self, envelope: EventEnvelope) -> EventPayload:
        """Validate ``envelope.payload`` into its typed model. Raises a contract error."""
        from pydantic import ValidationError

        model = self.model_for(envelope.event_type, envelope.event_version)
        try:
            return model.model_validate(envelope.payload)
        except ValidationError as exc:
            raise EventContractError(
                "the event payload failed validation against its registered model",
                extensions={"event_type": envelope.event_type, "errors": exc.error_count()},
            ) from exc


EVENT_REGISTRY = EventRegistry()


# --- P04-owned platform events ---------------------------------------------------------
# The event platform's own end-to-end self-verification events. They are platform-scoped
# diagnostics, not a business domain, and let integration tests and the lifecycle probe
# exercise the full outbox -> publish -> consume -> idempotency loop with a real contract.


@EVENT_REGISTRY.payload_model("platform.probe.emitted")
class PlatformProbeEmittedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "platform.probe.emitted"
    VERSION: ClassVar[int] = 1

    nonce: str
    note: str | None = None


@EVENT_REGISTRY.payload_model("platform.tenant_probe.emitted")
class PlatformTenantProbeEmittedV1(EventPayload):
    EVENT_TYPE: ClassVar[str] = "platform.tenant_probe.emitted"
    VERSION: ClassVar[int] = 1

    nonce: str
