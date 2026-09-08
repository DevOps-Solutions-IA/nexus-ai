"""Bounded, safe OpenAPI ingestion (NXS-INT-001, ADR-0057).

Importing an OpenAPI document does NOT auto-expose every endpoint. The pipeline is:

    parse (JSON, size-bounded)
      -> validate (OpenAPI 3.x shape, bounded nesting, bounded operation count)
      -> sanitize (reject remote/file ``$ref``, recursive ``$ref``, callbacks, webhooks,
         unsafe server URLs)
      -> extract allow-listed candidate operations (method + templated path + declared
         params + body flag)
      -> return the candidates for HUMAN selection.

Only the operations an operator explicitly selects are then registered (as normal
:class:`RestOperationSpec` rows). Nothing here performs a network fetch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import (
    DestinationRule,
    HttpMethod,
    ParamSpec,
    RestOperationSpec,
)
from nexus_ai.integrations.errors import IntegrationOpenApiInvalidError

_METHODS = {"get", "put", "post", "patch", "delete"}
_OPERATION_KEY_RE = re.compile(r"[^a-z0-9_.-]+")
_MAX_NESTING = 40


@dataclass(frozen=True, slots=True)
class CandidateOperation:
    operation_key: str
    method: HttpMethod
    path: str
    summary: str | None
    spec: RestOperationSpec


@dataclass(slots=True)
class OpenApiImportResult:
    title: str
    version: str
    server_url: str | None
    operations: list[CandidateOperation]
    warnings: list[str] = field(default_factory=list)


class OpenApiIngestor:
    def __init__(self, settings: IntegrationsSettings, policy: DestinationPolicy) -> None:
        self._settings = settings
        self._policy = policy

    def ingest(
        self, raw: bytes, *, destination_rule: DestinationRule | None = None
    ) -> OpenApiImportResult:
        if len(raw) > self._settings.openapi_max_document_bytes:
            raise IntegrationOpenApiInvalidError("the OpenAPI document exceeds the size limit")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise IntegrationOpenApiInvalidError(
                "the OpenAPI document is not valid JSON (YAML is not supported)"
            ) from exc
        if not isinstance(document, dict):
            raise IntegrationOpenApiInvalidError("the OpenAPI document is not an object")

        _assert_depth(document, self._settings.openapi_max_depth)
        self._reject_unsafe_refs(document)
        version = str(document.get("openapi", ""))
        if not version.startswith("3."):
            raise IntegrationOpenApiInvalidError("only OpenAPI 3.x documents are supported")
        if "callbacks" in json.dumps(document) or document.get("webhooks"):
            raise IntegrationOpenApiInvalidError(
                "OpenAPI callbacks / webhooks are not supported in an imported document"
            )

        info = document.get("info", {})
        title = str(info.get("title", "imported"))[:120]
        doc_version = str(info.get("version", "unknown"))[:64]
        server_url = self._safe_server_url(document, destination_rule)

        paths = document.get("paths", {})
        if not isinstance(paths, dict):
            raise IntegrationOpenApiInvalidError("the OpenAPI 'paths' object is invalid")

        result = OpenApiImportResult(
            title=title, version=doc_version, server_url=server_url, operations=[]
        )
        seen: set[str] = set()
        for path, item in paths.items():
            if not isinstance(path, str) or not path.startswith("/") or not isinstance(item, dict):
                result.warnings.append(f"skipped malformed path {path!r}")
                continue
            for method_name, operation in item.items():
                if method_name.lower() not in _METHODS or not isinstance(operation, dict):
                    continue
                if len(result.operations) >= self._settings.openapi_max_operations:
                    raise IntegrationOpenApiInvalidError(
                        "the OpenAPI document declares too many operations"
                    )
                candidate = self._candidate(path, method_name.lower(), operation, item, seen)
                seen.add(candidate.operation_key)
                result.operations.append(candidate)
        if not result.operations:
            raise IntegrationOpenApiInvalidError("no importable operations were found")
        return result

    # -- steps -----------------------------------------------------------------

    def _reject_unsafe_refs(self, node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#/"):
                raise IntegrationOpenApiInvalidError(
                    "only local in-document $ref values are allowed"
                )
            for value in node.values():
                self._reject_unsafe_refs(value)
        elif isinstance(node, list):
            for value in node:
                self._reject_unsafe_refs(value)

    def _safe_server_url(
        self, document: dict[str, Any], rule: DestinationRule | None
    ) -> str | None:
        servers = document.get("servers")
        if not isinstance(servers, list) or not servers:
            return None
        first = servers[0]
        if not isinstance(first, dict) or not isinstance(first.get("url"), str):
            return None
        url = str(first["url"])
        if "{" in url:  # server templating is not supported
            return None
        self._policy.validate_url(url, rule=rule)
        return url

    def _candidate(
        self,
        path: str,
        method: str,
        operation: dict[str, Any],
        path_item: dict[str, Any],
        seen: set[str],
    ) -> CandidateOperation:
        raw_key = operation.get("operationId") or f"{method}_{path.strip('/').replace('/', '_')}"
        key = _OPERATION_KEY_RE.sub("-", str(raw_key).lower()).strip("-._")[:63] or "op"
        while key in seen:
            key = f"{key}-x"[:63]

        template_names = set(re.findall(r"{([^{}/]+)}", path))
        parameters = list(path_item.get("parameters", [])) + list(operation.get("parameters", []))
        path_params: dict[str, ParamSpec] = {
            name: ParamSpec(required=True, max_length=256) for name in sorted(template_names)
        }
        query_params: dict[str, ParamSpec] = {}
        header_params: dict[str, ParamSpec] = {}
        for param in parameters:
            if not isinstance(param, dict):
                continue
            p_name, p_in = param.get("name"), param.get("in")
            if not isinstance(p_name, str):
                continue
            required = bool(param.get("required", False))
            if p_in == "query":
                query_params[p_name[:63]] = ParamSpec(required=required, max_length=1024)
            elif p_in == "header" and p_name.lower() not in {"authorization", "host", "cookie"}:
                header_params[p_name[:63]] = ParamSpec(required=required, max_length=1024)

        has_body = method != "get" and isinstance(operation.get("requestBody"), dict)
        body_schema = {"type": "object"} if has_body else None
        try:
            spec = RestOperationSpec(
                method=HttpMethod(method.upper()),
                path=path,
                path_params=path_params,
                query_params=query_params,
                header_params=header_params,
                body_schema=body_schema,
                retry_class=_retry_class_for(method),
            )
        except ValueError as exc:
            raise IntegrationOpenApiInvalidError(
                f"operation {method.upper()} {path} could not be represented safely"
            ) from exc
        summary = operation.get("summary")
        return CandidateOperation(
            operation_key=key,
            method=HttpMethod(method.upper()),
            path=path,
            summary=str(summary)[:200] if isinstance(summary, str) else None,
            spec=spec,
        )


def _retry_class_for(method: str) -> Any:
    from nexus_ai.integrations.entities import RetryClass

    if method == "get":
        return RetryClass.SAFE
    if method in ("put", "delete"):
        return RetryClass.IDEMPOTENT
    return RetryClass.NON_IDEMPOTENT


def _assert_depth(node: Any, limit: int, depth: int = 0) -> None:
    if depth > limit or depth > _MAX_NESTING * 4:
        raise IntegrationOpenApiInvalidError("the OpenAPI document is nested too deeply")
    if isinstance(node, dict):
        for value in node.values():
            _assert_depth(value, limit, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _assert_depth(value, limit, depth + 1)
