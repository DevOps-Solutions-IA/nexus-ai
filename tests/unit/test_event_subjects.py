"""Deterministic, injection-safe subject taxonomy (NXS-EVENT-002)."""

from __future__ import annotations

import pytest

from nexus_ai.events.errors import SubjectValidationError
from nexus_ai.events.subjects import (
    SubjectScope,
    build_subject,
    parse_subject,
    subscribe_filter,
)


def test_build_and_render() -> None:
    subject = build_subject(
        prefix="nxs",
        environment="production",
        scope=SubjectScope.TENANT,
        domain="organizations",
        event="profile.updated",
    )
    assert subject.value == "nxs.production.tenant.organizations.profile.updated"
    assert str(subject) == subject.value


@pytest.mark.parametrize(
    "field,value",
    [
        ("prefix", "NXS"),
        ("environment", "prod space"),
        ("domain", "org;drop"),
        ("domain", "org.sub"),
        ("event", "Created"),
        ("event", "created."),
        ("event", "a.b.c.d.e.f.g"),
    ],
)
def test_malformed_segments_are_rejected(field: str, value: str) -> None:
    kwargs = {
        "prefix": "nxs",
        "environment": "test",
        "scope": SubjectScope.GLOBAL,
        "domain": "platform",
        "event": "probe.emitted",
    }
    kwargs[field] = value
    with pytest.raises(SubjectValidationError):
        build_subject(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("wildcard", ["*", ">", "organizations.*", "some>thing"])
def test_wildcards_are_rejected_in_publish_subjects(wildcard: str) -> None:
    with pytest.raises(SubjectValidationError):
        build_subject(
            prefix="nxs",
            environment="test",
            scope=SubjectScope.TENANT,
            domain="organizations",
            event=wildcard,
        )


def test_parse_round_trip() -> None:
    original = "nxs.test.global.platform.probe.emitted"
    parsed = parse_subject(original)
    assert parsed.value == original
    assert parsed.scope is SubjectScope.GLOBAL


@pytest.mark.parametrize(
    "raw",
    [
        "nxs.test.tenant.organizations",  # too few segments
        "nxs.test.weird.organizations.created",  # unknown scope
        "nxs.test.tenant.Org.created",  # bad domain
        "",
        "nxs." * 100,
    ],
)
def test_parse_rejects_bad_subjects(raw: str) -> None:
    with pytest.raises(SubjectValidationError):
        parse_subject(raw)


def test_subscribe_filter_is_the_only_wildcard_producer() -> None:
    assert subscribe_filter(prefix="nxs", environment="test") == "nxs.test.>"
    assert (
        subscribe_filter(prefix="nxs", environment="test", scope=SubjectScope.TENANT)
        == "nxs.test.tenant.>"
    )
    assert (
        subscribe_filter(
            prefix="nxs", environment="test", scope=SubjectScope.TENANT, domain="organizations"
        )
        == "nxs.test.tenant.organizations.>"
    )


def test_subscribe_filter_requires_scope_for_domain() -> None:
    with pytest.raises(SubjectValidationError):
        subscribe_filter(prefix="nxs", environment="test", domain="organizations")
