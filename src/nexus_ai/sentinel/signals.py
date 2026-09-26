"""Source-owned bounded observation; no signal payload can select a destination."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid7

from nexus_ai.core.health import DependencyHealth, HealthStatus
from nexus_ai.sentinel.config import SentinelSettings
from nexus_ai.sentinel.contracts import Facts, Signal, digest
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sentinel.service import SentinelStore, SourceBinding


@dataclass(frozen=True)
class SignalAdapter:
    binding: SourceBinding
    probe: Callable[[], Awaitable[DependencyHealth]]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 30:
            raise SentinelDenied("adapter_timeout_bound")

    async def observe(self, observation_id: UUID) -> Signal:
        try:
            async with asyncio.timeout(self.timeout_seconds):
                health = await self.probe()
            condition = {
                HealthStatus.UP: "HEALTHY",
                HealthStatus.DOWN: "UNAVAILABLE",
                HealthStatus.DEGRADED: "DEGRADED",
                HealthStatus.UNKNOWN: "UNKNOWN",
            }[health.status]
            facts = Facts.model_validate({"condition": condition, "latency_ms": health.latency_ms})
        except Exception:
            facts = Facts(condition="UNKNOWN")
        return Signal(
            adapter_id=self.binding.adapter_id,
            adapter_revision=self.binding.adapter_revision,
            source_kind=self.binding.source_kind,
            source_identity=self.binding.source_identity,
            source_observation_id=str(observation_id),
            observed_at=datetime.now(UTC),
            subject_kind=self.binding.subject_kind,
            subject_id=self.binding.subject_id,
            organization_id=self.binding.organization_id,
            severity="INFO" if facts.condition == "HEALTHY" else "WARNING",
            fingerprint=digest([self.binding.adapter_id, "dependency_health"]),
            facts=facts,
        )


class SignalCollector:
    def __init__(
        self, store: SentinelStore, settings: SentinelSettings, adapters: tuple[SignalAdapter, ...]
    ) -> None:
        registry = {
            (item.binding.adapter_id, item.binding.source_identity): item for item in adapters
        }
        if len(registry) != len(adapters) or len(adapters) > settings.signal_batch_size:
            raise SentinelDenied("adapter_registry_bound")
        if any(store.sources.get(key) != item.binding for key, item in registry.items()):
            raise SentinelDenied("adapter_source_not_registered")
        self._store = store
        self._registry = MappingProxyType(registry)

    async def poll(self) -> tuple[tuple[UUID, UUID], ...]:
        receipts = []
        for adapter in self._registry.values():
            observation = await adapter.observe(uuid7())
            receipts.append(await self._store.ingest(observation))
        return tuple(receipts)


class NxsStateProbe:
    def __init__(self, repository: Path) -> None:
        self._path = repository / ".nxs/project-state.json"

    async def __call__(self) -> DependencyHealth:
        try:
            with self._path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("bounded_state")
            state = json.loads(raw)
            current = state["current_phase"]
            healthy = current["status"] in {"BUILDING", "VALIDATING", "READY", "PLANNED"}
        except OSError, ValueError, KeyError, TypeError:
            healthy = False
        return DependencyHealth("nxs", HealthStatus.UP if healthy else HealthStatus.DOWN, True)
