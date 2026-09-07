"""API contract primitives (NXS-API-001, sections 35)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_ai.api.contracts import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    PageParams,
    parse_idempotency_key,
)
from nexus_ai.core.errors import InvalidRequestError


def test_page_params_defaults_and_bounds() -> None:
    assert PageParams().limit == DEFAULT_PAGE_SIZE
    with pytest.raises(ValidationError):
        PageParams(limit=0)
    with pytest.raises(ValidationError):
        PageParams(limit=MAX_PAGE_SIZE + 1)
    with pytest.raises(ValidationError):
        PageParams(unexpected="x")  # type: ignore[call-arg]


def test_page_is_generic_and_frozen() -> None:
    page: Page[int] = Page(items=[1, 2, 3], limit=3, next_cursor="abc")
    assert page.items == [1, 2, 3]
    with pytest.raises(ValidationError):
        page.items = []  # type: ignore[misc]


@pytest.mark.parametrize("value", [None, "valid-key-000123", "  spaced-key-01  "])
def test_parse_idempotency_key_accepts_valid(value: str | None) -> None:
    result = parse_idempotency_key(value)
    if value is None:
        assert result is None
    else:
        assert result == value.strip()


@pytest.mark.parametrize("value", ["short", "has spaces 123456", "bad;chars;here12"])
def test_parse_idempotency_key_rejects_invalid(value: str) -> None:
    with pytest.raises(InvalidRequestError):
        parse_idempotency_key(value)
