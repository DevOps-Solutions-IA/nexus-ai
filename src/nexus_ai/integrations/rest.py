"""Build a governed outbound request from a REST operation spec + validated input
(NXS-INT-001).

The caller supplies a structured ``input`` — ``path_params``, ``query_params``,
``headers`` and ``body`` — never a URL, a method or a raw header block. Every path and
query value is checked against its allow-listed :class:`ParamSpec` (required / pattern /
length / constant), path values are percent-encoded into the template, and the body is
validated against the operation's JSON Schema. A header the operation did not declare, or
any reserved header, is refused.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker as _FormatChecker

from nexus_ai.integrations.entities import (
    RESERVED_REQUEST_HEADERS,
    HttpMethod,
    InvocationInput,
    ParamSpec,
    RestOperationSpec,
)
from nexus_ai.integrations.errors import IntegrationOperationInputInvalidError
from nexus_ai.integrations.executor import OutboundRequest

_MAX_BODY_BYTES = 1_048_576


def parse_input(raw: dict[str, Any]) -> InvocationInput:
    try:
        return InvocationInput.model_validate(raw)
    except ValueError as exc:
        raise IntegrationOperationInputInvalidError(
            "the operation input is not shaped as {path_params, query_params, headers, body}"
        ) from exc


def _coerce_scalar(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise IntegrationOperationInputInvalidError(
        f"parameter {name!r} must be a string, number or boolean"
    )


def _validate_param(name: str, spec: ParamSpec, provided: dict[str, Any]) -> str | None:
    if spec.constant is not None:
        return spec.constant
    if name not in provided or provided[name] is None:
        if spec.required:
            raise IntegrationOperationInputInvalidError(f"parameter {name!r} is required")
        return None
    text = _coerce_scalar(name, provided[name])
    if len(text) > spec.max_length:
        raise IntegrationOperationInputInvalidError(f"parameter {name!r} is too long")
    if spec.pattern is not None:
        import re

        if not re.fullmatch(spec.pattern, text):
            raise IntegrationOperationInputInvalidError(
                f"parameter {name!r} does not match its allowed pattern"
            )
    return text


def _reject_unknown(kind: str, provided: dict[str, Any], allowed: set[str]) -> None:
    extra = set(provided) - allowed
    if extra:
        raise IntegrationOperationInputInvalidError(f"unknown {kind}: {', '.join(sorted(extra))}")


class RestInvocationBuilder:
    def build(
        self, base_url: str, spec: RestOperationSpec, raw_input: dict[str, Any]
    ) -> OutboundRequest:
        supplied = parse_input(raw_input)

        _reject_unknown("path parameter", supplied.path_params, set(spec.path_params))
        _reject_unknown("query parameter", supplied.query_params, set(spec.query_params))
        _reject_unknown("header", supplied.headers, set(spec.header_params))

        path = spec.path
        for name, param_spec in spec.path_params.items():
            value = _validate_param(name, param_spec, supplied.path_params)
            if value is None:  # pragma: no cover - path params are required by template match
                raise IntegrationOperationInputInvalidError(f"path parameter {name!r} is required")
            path = path.replace(f"{{{name}}}", quote(value, safe=""))

        query_pairs: list[tuple[str, str]] = []
        for name, param_spec in spec.query_params.items():
            value = _validate_param(name, param_spec, supplied.query_params)
            if value is not None:
                query_pairs.append((name, value))

        headers: dict[str, str] = {}
        for name, param_spec in spec.header_params.items():
            value = _validate_param(name, param_spec, supplied.headers)
            if value is not None:
                if name.lower() in RESERVED_REQUEST_HEADERS:  # pragma: no cover - spec-validated
                    raise IntegrationOperationInputInvalidError(f"header {name!r} is reserved")
                headers[name] = value

        body_bytes, content_type = self._body(spec, supplied.body)

        url = build_url(base_url, path, query_pairs)
        return OutboundRequest(
            method=spec.method.value,
            url=url,
            headers=headers,
            body=body_bytes,
            content_type=content_type,
            timeout_seconds=spec.timeout_seconds,
        )

    def _body(self, spec: RestOperationSpec, body: Any | None) -> tuple[bytes | None, str | None]:
        if spec.method is HttpMethod.GET or spec.body_schema is None:
            if body is not None:
                raise IntegrationOperationInputInvalidError(
                    "this operation does not accept a request body"
                )
            return None, None
        if body is None:
            raise IntegrationOperationInputInvalidError("this operation requires a request body")
        validator = Draft202012Validator(spec.body_schema, format_checker=_FormatChecker())
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
        if errors:
            raise IntegrationOperationInputInvalidError(
                "the request body failed schema validation",
                errors=[
                    {"path": "/".join(str(p) for p in e.path), "message": e.message}
                    for e in errors[:10]
                ],
            )
        encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_BODY_BYTES:
            raise IntegrationOperationInputInvalidError("the request body is too large")
        return encoded, "application/json"


def build_url(base_url: str, path: str, query_pairs: list[tuple[str, str]]) -> str:
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    full_path = f"{base_path}{path}" if path.startswith("/") else f"{base_path}/{path}"
    query = urlencode(query_pairs) if query_pairs else ""
    return urlunsplit((base.scheme, base.netloc, full_path, query, ""))
