"""Identifiers, request context and error/problem-details primitives."""

from __future__ import annotations

import asyncio

import pytest

from nexus_ai.core.context import current_context, request_context
from nexus_ai.core.errors import DependencyUnavailableError, NotFoundError, ValidationFailedError
from nexus_ai.core.identifiers import is_acceptable, new_id, sanitize
from nexus_ai.core.problem_details import from_error, unexpected


def test_new_id_is_unique_hex() -> None:
    ids = {new_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(len(value) == 32 and int(value, 16) >= 0 for value in ids)


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("valid-request-1234", True),
        ("  spaced-value-1234  ", True),
        ("short", False),
        ("has spaces here now", False),
        ("x" * 400, False),
        (None, False),
        ("bad;semicolon-1234", False),
    ],
)
def test_sanitize(raw: str | None, kept: bool) -> None:
    result = sanitize(raw)
    if kept:
        assert result == (raw or "").strip()
    else:
        assert len(result) == 32
        assert not is_acceptable("short")


def test_request_context_isolation_between_tasks() -> None:
    async def worker(tag: str) -> tuple[str, str]:
        with request_context(request_id=f"req-{tag}-0001", correlation_id=f"cor-{tag}-0001"):
            await asyncio.sleep(0)
            context = current_context()
            assert context is not None
            return context.request_id, context.correlation_id

    async def main() -> None:
        results = await asyncio.gather(*(worker(str(index)) for index in range(50)))
        assert len({request_id for request_id, _ in results}) == 50
        assert current_context() is None

    asyncio.run(main())


def test_problem_details_from_error_shape() -> None:
    with request_context(request_id="req-abcdef-0001", correlation_id="cor-abcdef-0001"):
        problem = from_error(NotFoundError("No such widget."), instance="/api/v1/widgets/9")
    assert problem.status == 404
    assert problem.code == "NXS_CORE_NOT_FOUND"
    assert problem.type.endswith("NXS_CORE_NOT_FOUND")
    assert problem.request_id == "req-abcdef-0001"
    assert problem.instance == "/api/v1/widgets/9"


def test_problem_details_marks_retryable() -> None:
    problem = from_error(DependencyUnavailableError("db down"))
    dumped = problem.model_dump()
    assert dumped["retryable"] is True
    assert problem.status == 503


def test_validation_error_carries_field_errors() -> None:
    problem = from_error(ValidationFailedError([{"field": "name", "message": "required"}]))
    assert problem.model_dump()["errors"] == [{"field": "name", "message": "required"}]


def test_unexpected_problem_is_generic() -> None:
    problem = unexpected(instance="/boom")
    assert problem.status == 500
    assert problem.code == "NXS_CORE_INTERNAL"
    assert "unexpected" in problem.detail.lower()
