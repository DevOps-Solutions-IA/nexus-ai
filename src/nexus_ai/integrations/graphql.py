"""Governed GraphQL primitive (NXS-INT-001, ADR-0058).

GraphQL is executed ONLY as a fixed, pre-registered document. The caller supplies
variables — never a query string. At registration the document is analysed with a bounded
brace-depth scanner (Nexus does not embed a GraphQL engine): selection nesting must be
within ``max_depth``; introspection (``__schema`` / ``__type``) is refused; a
``mutation`` / ``subscription`` operation is refused unless the operation explicitly
enables mutations. At execution the variables are schema-validated and the request is a
single bounded POST.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker as _FormatChecker

from nexus_ai.integrations.entities import GraphQLOperationSpec
from nexus_ai.integrations.errors import (
    IntegrationGraphQLInvalidError,
    IntegrationOperationInputInvalidError,
)
from nexus_ai.integrations.executor import OutboundRequest

_INTROSPECTION = re.compile(r"__(schema|type)\b")
_OPERATION_KEYWORD = re.compile(r"\b(query|mutation|subscription)\b")
_COMMENT = re.compile(r"#[^\n]*")
_STRING = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"')


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    max_depth: int
    has_mutation: bool


def analyse_document(document: str, *, max_depth_limit: int) -> DocumentInfo:
    """Structural safety analysis of a GraphQL document. Raises on any violation."""
    stripped = _STRING.sub('""', _COMMENT.sub("", document))
    if _INTROSPECTION.search(stripped):
        raise IntegrationGraphQLInvalidError(
            "introspection is not permitted in a registered document"
        )
    keywords = {m.group(1) for m in _OPERATION_KEYWORD.finditer(stripped)}
    if "subscription" in keywords:
        raise IntegrationGraphQLInvalidError("subscriptions are not supported")
    has_mutation = "mutation" in keywords

    depth = 0
    max_seen = 0
    for char in stripped:
        if char == "{":
            depth += 1
            max_seen = max(max_seen, depth)
        elif char == "}":
            depth -= 1
            if depth < 0:
                raise IntegrationGraphQLInvalidError("the GraphQL document has unbalanced braces")
    if depth != 0:
        raise IntegrationGraphQLInvalidError("the GraphQL document has unbalanced braces")
    # The outermost brace is the operation body; selection depth is one less.
    selection_depth = max(0, max_seen - 1)
    if selection_depth > max_depth_limit:
        raise IntegrationGraphQLInvalidError(
            f"selection depth {selection_depth} exceeds the limit {max_depth_limit}"
        )
    return DocumentInfo(max_depth=selection_depth, has_mutation=has_mutation)


def validate_operation_spec(spec: GraphQLOperationSpec, *, max_depth_limit: int) -> None:
    info = analyse_document(spec.document, max_depth_limit=min(spec.max_depth, max_depth_limit))
    if info.has_mutation and not spec.allow_mutation:
        raise IntegrationGraphQLInvalidError(
            "this operation contains a mutation but allow_mutation is false"
        )


class GraphQLInvocationBuilder:
    def build(
        self, base_url: str, spec: GraphQLOperationSpec, raw_input: dict[str, Any]
    ) -> OutboundRequest:
        variables = raw_input.get("variables", {})
        if not isinstance(variables, dict):
            raise IntegrationOperationInputInvalidError("GraphQL 'variables' must be an object")
        extra = set(raw_input) - {"variables"}
        if extra:
            raise IntegrationOperationInputInvalidError(
                f"unexpected GraphQL input keys: {', '.join(sorted(extra))}"
            )
        if spec.variables_schema is not None:
            validator = Draft202012Validator(spec.variables_schema, format_checker=_FormatChecker())
            errors = sorted(validator.iter_errors(variables), key=lambda e: list(e.path))
            if errors:
                raise IntegrationOperationInputInvalidError(
                    "the GraphQL variables failed schema validation",
                    errors=[
                        {"path": "/".join(str(p) for p in e.path), "message": e.message}
                        for e in errors[:10]
                    ],
                )
        payload: dict[str, Any] = {"query": spec.document, "variables": variables}
        if spec.operation_name:
            payload["operationName"] = spec.operation_name
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return OutboundRequest(
            method="POST",
            url=base_url,
            headers={"Accept": "application/json"},
            body=body,
            content_type="application/json",
            timeout_seconds=spec.timeout_seconds,
        )
