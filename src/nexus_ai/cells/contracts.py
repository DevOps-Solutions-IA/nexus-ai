"""Bounded placement contracts; a resolved snapshot is never an execution permit."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SafeKey = Annotated[
    str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9:_-]+$")
]
ReasonCode = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
]
Generation = Annotated[int, Field(strict=True, ge=1, le=9_223_372_036_854_775_807)]


class CellState(StrEnum):
    REGISTERED = "REGISTERED"
    RETIRED = "RETIRED"


class PlacementState(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class PlacementOperation(StrEnum):
    ASSIGN = "ASSIGN"
    SUSPEND = "SUSPEND"
    RESUME = "RESUME"


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RegisterCellRequest(StrictContract):
    cell_key: Annotated[
        str, StringConstraints(min_length=1, max_length=48, pattern=r"^[a-z][a-z0-9-]*$")
    ]
    idempotency_key: SafeKey
    reason_code: ReasonCode


class PlacementMutation(StrictContract):
    cell_id: UUID
    operation: PlacementOperation
    expected_generation: Generation | None = None
    idempotency_key: SafeKey
    reason_code: ReasonCode

    @model_validator(mode="after")
    def validate_generation(self) -> PlacementMutation:
        if (self.operation is PlacementOperation.ASSIGN) != (self.expected_generation is None):
            raise ValueError("ASSIGN requires no generation; SUSPEND and RESUME require one")
        return self

    def fingerprint(self) -> str:
        return semantic_fingerprint(self.model_dump(mode="json", exclude={"idempotency_key"}))


class RetireCellRequest(StrictContract):
    cell_id: UUID
    idempotency_key: SafeKey
    reason_code: ReasonCode


class CellView(StrictContract):
    id: UUID
    cell_key: str
    state: CellState


class PlacementSnapshot(StrictContract):
    organization_id: UUID
    placement_id: UUID
    cell_id: UUID
    assignment_generation: Generation
    state: PlacementState


class PlacementPage(StrictContract):
    after_id: UUID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


def semantic_fingerprint(value: dict[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
