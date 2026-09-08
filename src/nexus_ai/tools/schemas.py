"""Bounded, deterministic tool-argument schemas (NXS-TOOL-001, ADR-0062).

A tool's ``input_schema`` and ``output_schema`` are stored JSONB. They are NEVER
executable code and are NEVER evaluated as expressions — they are Draft 2020-12 JSON
Schemas, validated on registration for shape and bounds:

* valid against the metaschema;
* size-bounded (serialised bytes) and nesting-bounded;
* only local (in-document) ``$ref``;
* the ``input_schema`` must be a closed object (``type: object`` +
  ``additionalProperties: false``) so an invocation cannot smuggle an unexpected
  argument;
* the ``output_schema`` (optional) is bounded but may be open, since an external
  response shape is not fully under Nexus's control.

At invocation the caller's ``arguments`` are validated against the compiled input schema;
anything that fails is a stable ``NXS_TOOL_ARGS_INVALID``.
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import FormatChecker as _FormatChecker
from jsonschema.exceptions import SchemaError

from nexus_ai.tools.errors import ToolArgumentsInvalidError, ToolConfigInvalidError

_MAX_SCHEMA_BYTES = 65_536
_MAX_SCHEMA_DEPTH = 12
_MAX_ARGUMENT_BYTES = 262_144


def _assert_no_remote_ref(node: Any, depth: int = 0) -> None:
    if depth > _MAX_SCHEMA_DEPTH:
        raise ToolConfigInvalidError("the schema is nested too deeply")
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            raise ToolConfigInvalidError("only local in-document $ref is allowed in a tool schema")
        for value in node.values():
            _assert_no_remote_ref(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _assert_no_remote_ref(value, depth + 1)


def validate_schema_document(schema: dict[str, Any], *, closed_object: bool) -> None:
    """Validate a stored tool schema for shape and bounds. Raises ``NXS_TOOL_CONFIG_INVALID``."""
    if not isinstance(schema, dict):
        raise ToolConfigInvalidError("a tool schema must be a JSON object")
    encoded = json.dumps(schema, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise ToolConfigInvalidError("the tool schema exceeds the size limit")
    _assert_no_remote_ref(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ToolConfigInvalidError(
            f"the tool schema is not a valid JSON Schema: {exc.message}"
        ) from exc
    if closed_object:
        if schema.get("type") != "object":
            raise ToolConfigInvalidError("a tool input_schema must be an object schema")
        if schema.get("additionalProperties", True) is not False:
            raise ToolConfigInvalidError(
                "a tool input_schema must set additionalProperties: false (closed object)"
            )


def compile_validator(schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(schema, format_checker=_FormatChecker())


def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate caller arguments against the tool's input schema. Raises
    ``NXS_TOOL_ARGS_INVALID``."""
    encoded = json.dumps(arguments, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
        raise ToolArgumentsInvalidError("the invocation arguments exceed the size limit")
    validator = compile_validator(schema)
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
    if errors:
        raise ToolArgumentsInvalidError(
            "the invocation arguments failed the tool input schema",
            errors=[
                {"path": "/".join(str(p) for p in e.path), "message": e.message}
                for e in errors[:10]
            ],
        )


def validate_output(value: Any, schema: dict[str, Any] | None) -> None:
    """Validate a downstream result against the tool's output schema. Raises
    ``NXS_TOOL_RESULT_INVALID``."""
    if schema is None:
        return
    from nexus_ai.tools.errors import ToolResultInvalidError

    validator = compile_validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    if errors:
        raise ToolResultInvalidError("the downstream result failed the tool output schema")


def merge_arguments(
    caller_arguments: dict[str, Any], static_arguments: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge ``static_arguments`` OVER ``caller_arguments`` — a static value the tool
    author fixed can never be overridden by the caller (nor by a model)."""

    def _merge(base: Any, overlay: Any) -> Any:
        if isinstance(base, dict) and isinstance(overlay, dict):
            out = dict(base)
            for key, value in overlay.items():
                out[key] = _merge(out.get(key), value) if key in out else value
            return out
        return overlay

    merged = _merge(caller_arguments, static_arguments)
    return merged if isinstance(merged, dict) else dict(caller_arguments)
