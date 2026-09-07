"""Deterministic, tenant-safe NATS subject taxonomy (NXS-EVENT-002).

Canonical shape::

    <prefix>.<environment>.<scope>.<domain>.<event>

* ``prefix`` — the platform namespace (``nxs``)
* ``environment`` — ``local`` / ``test`` / ``staging`` / ``production``
* ``scope`` — ``tenant`` for Organization-owned events, ``global`` for platform events
* ``domain`` — the producing domain (``organizations``, ``auth`` …)
* ``event`` — a dotted, past-tense event name (``created``, ``profile.updated``)

Subjects never carry a tenant identifier, an email, a phone number or any other
caller-entered value — those are injection and PII risks. Wildcards are rejected by the
publish builder; :func:`subscribe_filter` is the only place a controlled ``>`` is
produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from nexus_ai.events.errors import SubjectValidationError

_TOKEN = re.compile(r"\A[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_EVENT = re.compile(r"\A[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*){0,4}\Z")
_MAX_SUBJECT_LEN = 240
_MAX_TOKEN_LEN = 48


class SubjectScope(StrEnum):
    TENANT = "tenant"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class Subject:
    prefix: str
    environment: str
    scope: SubjectScope
    domain: str
    event: str

    def __str__(self) -> str:
        return ".".join([self.prefix, self.environment, self.scope.value, self.domain, self.event])

    @property
    def value(self) -> str:
        return str(self)


def _token(value: str, label: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_TOKEN_LEN or not _TOKEN.match(candidate):
        raise SubjectValidationError(f"unsafe subject {label} segment: {value!r}")
    return candidate


def _event_token(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_TOKEN_LEN * 3 or not _EVENT.match(candidate):
        raise SubjectValidationError(f"unsafe subject event segment: {value!r}")
    return candidate


def build_subject(
    *,
    prefix: str,
    environment: str,
    scope: SubjectScope | str,
    domain: str,
    event: str,
) -> Subject:
    """Build a fully-qualified publish subject. Raises on any malformed segment.

    A ``*`` or ``>`` anywhere is rejected: a publish subject is always concrete.
    """
    resolved_scope = scope if isinstance(scope, SubjectScope) else SubjectScope(scope)
    subject = Subject(
        prefix=_token(prefix, "prefix"),
        environment=_token(environment, "environment"),
        scope=resolved_scope,
        domain=_token(domain, "domain"),
        event=_event_token(event),
    )
    rendered = str(subject)
    if len(rendered) > _MAX_SUBJECT_LEN:
        raise SubjectValidationError("subject exceeds the maximum length")
    if "*" in rendered or ">" in rendered or " " in rendered:
        raise SubjectValidationError("publish subjects must not contain wildcards or spaces")
    return subject


def parse_subject(raw: str) -> Subject:
    """Parse and validate a concrete subject string back into a :class:`Subject`."""
    if not raw or len(raw) > _MAX_SUBJECT_LEN:
        raise SubjectValidationError("subject is empty or too long")
    parts = raw.split(".")
    if len(parts) < 5:
        raise SubjectValidationError(f"subject does not match the canonical taxonomy: {raw!r}")
    prefix, environment, scope, domain, *event_parts = parts
    try:
        resolved_scope = SubjectScope(scope)
    except ValueError as exc:
        raise SubjectValidationError(f"unknown subject scope: {scope!r}") from exc
    return build_subject(
        prefix=prefix,
        environment=environment,
        scope=resolved_scope,
        domain=domain,
        event=".".join(event_parts),
    )


def subscribe_filter(
    *,
    prefix: str,
    environment: str,
    scope: SubjectScope | str | None = None,
    domain: str | None = None,
) -> str:
    """A controlled subscription filter. The ONLY producer of a trailing ``>``.

    ``scope``/``domain`` narrow the filter left-to-right; the remainder is ``>``.
    """
    segments = [_token(prefix, "prefix"), _token(environment, "environment")]
    if scope is not None:
        resolved = scope if isinstance(scope, SubjectScope) else SubjectScope(scope)
        segments.append(resolved.value)
        if domain is not None:
            segments.append(_token(domain, "domain"))
    elif domain is not None:
        raise SubjectValidationError("a domain filter requires an explicit scope")
    segments.append(">")
    return ".".join(segments)
