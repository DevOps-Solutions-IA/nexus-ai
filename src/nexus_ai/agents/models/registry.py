"""Model provider adapter registry (NXS-P13, ADR-0091)."""

from __future__ import annotations

from nexus_ai.agents.models.base import ModelProviderAdapter
from nexus_ai.agents.models.fake import FakeModelProvider
from nexus_ai.agents.models.openai_compatible import OpenAiCompatibleModelProvider

_STATELESS: dict[str, ModelProviderAdapter] = {
    "openai_compatible": OpenAiCompatibleModelProvider(),
}


def resolve_model_provider(key: str) -> ModelProviderAdapter:
    """Resolve a production adapter by its stable key. The ``fake`` provider is stateful
    and test-scoped — it is created per session, never shared here."""
    if key == "fake":
        return FakeModelProvider()
    try:
        return _STATELESS[key]
    except KeyError:
        raise LookupError(f"unknown model provider {key!r}") from None


def known_model_providers() -> tuple[str, ...]:
    return tuple(sorted({"fake", *_STATELESS}))
