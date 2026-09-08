"""Untrusted response validation and normalisation into an IntegrationResult
(NXS-INT-001, ADR-0059).

An upstream response is untrusted input. Before any value reaches a caller it must pass,
in order: HTTP status classification, a content-type check, a body-size check (already
bounded by the executor), a strict JSON parse, and — when the operation declares one — a
JSON Schema check with required-field and type enforcement. Anything that fails becomes a
stable ``NXS_INT_RESPONSE_INVALID`` (or the matching upstream-error code); nothing is
half-parsed and handed on.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker as _FormatChecker

from nexus_ai.integrations.entities import RestOperationSpec
from nexus_ai.integrations.errors import (
    IntegrationResponseInvalidError,
    IntegrationUpstreamClientError,
    IntegrationUpstreamRateLimitedError,
    IntegrationUpstreamServerError,
)
from nexus_ai.integrations.executor import RawResponse

_MAX_JSON_TEXT = 8 * 1024 * 1024


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 86_400 else None


def classify_status(response: RawResponse) -> None:
    """Raise the matching upstream error for a non-2xx status; return for 2xx/3xx."""
    status = response.status_code
    if status == 429:
        raise IntegrationUpstreamRateLimitedError(
            "the upstream rate limited the request",
            retry_after_seconds=parse_retry_after(response.headers.get("retry-after")),
        )
    if 500 <= status <= 599:
        raise IntegrationUpstreamServerError(
            "the upstream returned a server error", upstream_status=status
        )
    if 400 <= status <= 499:
        raise IntegrationUpstreamClientError(
            "the upstream rejected the request", upstream_status=status
        )


def _content_type_matches(actual: str | None, expected: str) -> bool:
    if not expected:
        return True
    if actual is None:
        return False
    return actual.split(";", 1)[0].strip().lower() == expected.split(";", 1)[0].strip().lower()


def validate_rest_response(response: RawResponse, spec: RestOperationSpec) -> Any:
    """Validate a raw response against the operation spec and return the decoded output."""
    if response.status_code not in spec.success_status:
        classify_status(response)
        raise IntegrationResponseInvalidError(
            f"unexpected upstream status {response.status_code}", reason="unexpected_status"
        )
    if response.status_code == 204 or not response.body:
        _check_schema(None, spec.response_schema)
        return None
    if not _content_type_matches(response.headers.get("content-type"), spec.response_content_type):
        raise IntegrationResponseInvalidError(
            "the upstream returned an unexpected content-type", reason="content_type"
        )
    if len(response.body) > _MAX_JSON_TEXT:
        raise IntegrationResponseInvalidError(
            "the upstream response is too large to parse", reason="too_large"
        )
    try:
        decoded = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IntegrationResponseInvalidError(
            "the upstream response is not valid JSON", reason="invalid_json"
        ) from exc
    _check_schema(decoded, spec.response_schema)
    return decoded


def validate_graphql_response(response: RawResponse, response_schema: dict[str, Any] | None) -> Any:
    if response.status_code != 200:
        classify_status(response)
        raise IntegrationResponseInvalidError(
            f"GraphQL endpoint returned status {response.status_code}", reason="unexpected_status"
        )
    try:
        decoded = json.loads(response.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise IntegrationResponseInvalidError(
            "the GraphQL response is not valid JSON", reason="invalid_json"
        ) from exc
    if not isinstance(decoded, dict):
        raise IntegrationResponseInvalidError(
            "the GraphQL response is not an object", reason="not_object"
        )
    if decoded.get("errors"):
        raise IntegrationResponseInvalidError(
            "the GraphQL response reported errors", reason="graphql_errors"
        )
    data = decoded.get("data")
    _check_schema(data, response_schema)
    return data


def _check_schema(value: Any, schema: dict[str, Any] | None) -> None:
    if schema is None:
        return
    validator = Draft202012Validator(schema, format_checker=_FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    if errors:
        raise IntegrationResponseInvalidError(
            "the upstream response failed output-schema validation",
            reason="schema_violation",
        )
