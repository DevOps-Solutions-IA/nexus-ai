"""Immutable source registry; persisted metadata cannot manufacture handlers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from nexus_ai.sentinel.contracts import (
    Parameters,
    Proposal,
    Risk,
    Runbook,
    StrictModel,
    Subject,
    digest,
)
from nexus_ai.sentinel.errors import SentinelDenied


@dataclass(frozen=True)
class HandlerBinding:
    key: str
    revision: int
    risk: Risk
    non_mutating: bool
    targets: tuple[tuple[Subject, UUID, int | None], ...]

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "key": self.key,
                "revision": self.revision,
                "risk": self.risk,
                "non_mutating": self.non_mutating,
                "targets": sorted(
                    (kind.value, str(identity), generation)
                    for kind, identity, generation in self.targets
                ),
            }
        )


class ActionResult(StrictModel):
    classification: Literal["OBSERVED", "DIAGNOSED", "APPLIED", "REJECTED"]
    external_reference: UUID | None = None


class DispatchRequest(StrictModel):
    target_kind: Subject
    target_id: UUID
    target_generation: int | None
    parameters: Parameters
    idempotency_key: str = Field(min_length=1, max_length=128)


class SentinelActionAdapter(Protocol):
    binding: HandlerBinding

    async def execute(self, request: DispatchRequest) -> ActionResult: ...


class ProvenPreEffectRejection(Exception):
    """Only a trusted adapter that proves zero external effect may raise this."""


class SentinelRunbookRegistry:
    def __init__(self, adapters: tuple[SentinelActionAdapter, ...]) -> None:
        if len(adapters) > 256:
            raise SentinelDenied("handler_registry_bound")
        mapping = {(adapter.binding.key, adapter.binding.revision): adapter for adapter in adapters}
        if len(mapping) != len(adapters):
            raise SentinelDenied("duplicate_handler")
        for adapter in adapters:
            binding = adapter.binding
            if not 1 <= binding.revision <= 2_147_483_647 or not 1 <= len(binding.targets) <= 32:
                raise SentinelDenied("handler_target_bound")
            if len(set((kind, identity) for kind, identity, _ in binding.targets)) != len(
                binding.targets
            ):
                raise SentinelDenied("duplicate_handler_target")
            if binding.risk in {Risk.HIGH_IMPACT, Risk.DESTRUCTIVE}:
                raise SentinelDenied("handler_risk_denied")
            if binding.risk in {Risk.OBSERVE, Risk.DIAGNOSTIC} and not binding.non_mutating:
                raise SentinelDenied("handler_risk_mismatch")
            if not binding.non_mutating and any(
                kind == Subject.ORGANIZATION for kind, _, _ in binding.targets
            ):
                raise SentinelDenied("tenant_mutation_denied")
        self._adapters = MappingProxyType(mapping)

    def resolve(self, book: Runbook, proposal: Proposal) -> SentinelActionAdapter:
        adapter = self._adapters.get((book.handler_key, book.handler_revision))
        if adapter is None:
            raise SentinelDenied("unknown_handler")
        binding = adapter.binding
        if (
            not book.enabled
            or book.handler_binding_digest != binding.fingerprint
            or book.risk != binding.risk
            or book.non_mutating != binding.non_mutating
            or (proposal.target_kind, proposal.target_id, proposal.target_generation)
            not in binding.targets
            or proposal.risk != binding.risk
        ):
            raise SentinelDenied("handler_target_or_semantics_mismatch")
        return adapter


class DiagnosticActionAdapter:
    def __init__(self, binding: HandlerBinding, probe: Callable[[], Awaitable[object]]) -> None:
        if (
            not binding.non_mutating
            or binding.risk not in {Risk.OBSERVE, Risk.DIAGNOSTIC}
            or len(binding.targets) != 1
        ):
            raise SentinelDenied("diagnostic_handler_bound")
        self.binding = binding
        self._probe = probe

    async def execute(self, request: DispatchRequest) -> ActionResult:
        if (
            request.target_kind,
            request.target_id,
            request.target_generation,
        ) not in self.binding.targets:
            raise ProvenPreEffectRejection()
        await self._probe()
        return ActionResult(classification="DIAGNOSED")
