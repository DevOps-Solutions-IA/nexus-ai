"""Closed P18 inputs do not confer tenant or execution authority."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.cells.contracts import (
    PlacementMutation,
    PlacementOperation,
    PlacementPage,
    RegisterCellRequest,
    semantic_fingerprint,
)


def test_mutation_fingerprint_is_stable_and_key_independent() -> None:
    request = PlacementMutation(
        cell_id=uuid4(), operation="ASSIGN", idempotency_key="initial", reason_code="ONBOARDING"
    )
    replay = request.model_copy(update={"idempotency_key": "another"})
    assert request.fingerprint() == replay.fingerprint()
    assert len(request.fingerprint()) == 64
    assert request.fingerprint() != request.model_copy(update={"cell_id": uuid4()}).fingerprint()
    assert semantic_fingerprint({"a": 1, "b": 2}) == semantic_fingerprint({"b": 2, "a": 1})


@pytest.mark.parametrize(
    ("operation", "generation"),
    [("ASSIGN", 1), ("SUSPEND", None), ("RESUME", None), ("RESUME", 0), ("RESUME", True)],
)
def test_mutation_rejects_invalid_generation(operation: str, generation: object) -> None:
    with pytest.raises(ValidationError):
        PlacementMutation.model_validate(
            dict(
                cell_id=str(uuid4()),
                operation=operation,
                expected_generation=generation,
                idempotency_key="key",
                reason_code="CONTROL",
            )
        )


@pytest.mark.parametrize("operation", [PlacementOperation.SUSPEND, PlacementOperation.RESUME])
def test_transition_requires_bounded_expected_generation(operation: PlacementOperation) -> None:
    request = PlacementMutation(
        cell_id=uuid4(),
        operation=operation,
        expected_generation=2,
        idempotency_key="key",
        reason_code="CONTROL",
    )
    assert request.expected_generation == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", str(uuid4())),
        ("url", "https://example.test"),
        ("idempotency_key", "x" * 129),
        ("reason_code", "unsafe\nreason"),
        ("operation", "REASSIGN"),
        ("cell_id", "bad-uuid"),
    ],
)
def test_mutation_rejects_spoofed_or_unbounded_input(field: str, value: str) -> None:
    payload = dict(
        cell_id=str(uuid4()), operation="ASSIGN", idempotency_key="key", reason_code="CONTROL"
    )
    payload[field] = value
    with pytest.raises(ValidationError):
        PlacementMutation.model_validate(payload)


@pytest.mark.parametrize("key", ["UPPER", "bad key", "a" * 49, "https://host", "", "$(shell)"])
def test_cell_key_is_closed(key: str) -> None:
    with pytest.raises(ValidationError):
        RegisterCellRequest(cell_key=key, idempotency_key="key", reason_code="REGISTER")


@pytest.mark.parametrize("limit", [0, 101, True, -1])
def test_page_is_bounded(limit: int) -> None:
    with pytest.raises(ValidationError):
        PlacementPage(limit=limit)


@pytest.mark.parametrize(
    "payload",
    [
        {"after_id": "not-a-uuid"},
        {"after_id": "a" * 10000},
        {"offset": 1},
        {"limit": "100"},
        {"limit": 1000000},
    ],
)
def test_malformed_cursor_and_unbounded_page_fail_closed(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PlacementPage.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("metadata", {"url": "https://untrusted.example"}),
        ("connection_string", "secret"),
        ("reason_code", "X" * 65),
        ("idempotency_key", "X" * 129),
    ],
)
def test_inventory_input_has_no_arbitrary_metadata(field: str, value: object) -> None:
    payload: dict[str, object] = {
        "cell_key": "cell",
        "idempotency_key": "key",
        "reason_code": "REGISTER",
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        RegisterCellRequest.model_validate(payload)


def test_worker_configuration_is_frozen_and_not_http_input() -> None:
    from nexus_ai.core.config import CellSettings

    settings = CellSettings(worker_cell_id=uuid4())
    with pytest.raises(ValidationError):
        settings.worker_cell_id = uuid4()
    with pytest.raises(ValidationError):
        CellSettings.model_validate({"X-Cell-ID": str(uuid4())})
