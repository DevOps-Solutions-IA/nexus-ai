"""Static tenant-schema guard branches (NXS-TENANT-003, section 28)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Column, ForeignKey, Integer, String, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from nexus_ai.infrastructure.orm import TENANT_OWNED, TENANT_SCOPED_KEY, TENANT_SELF, metadata
from nexus_ai.infrastructure.schema_guard import check_static


@pytest.fixture
def probe_table() -> Iterator[Callable[..., Table]]:
    created: list[Table] = []

    def _make(name: str, *columns: Column, marker: str = TENANT_OWNED) -> Table:
        table = Table(name, metadata, *columns, info={TENANT_SCOPED_KEY: marker})
        created.append(table)
        return table

    yield _make
    for table in created:
        metadata.remove(table)


def _violations_for(name: str) -> list[str]:
    return [v for v in check_static().violations if v.startswith(name)]


def test_missing_organization_id(probe_table) -> None:
    probe_table("g_missing", Column("id", Integer, primary_key=True))
    assert any("missing organization_id" in v for v in _violations_for("g_missing"))


def test_nullable_organization_id(probe_table) -> None:
    probe_table(
        "g_nullable",
        Column("id", Integer, primary_key=True),
        Column("organization_id", PgUUID(as_uuid=True), ForeignKey("organizations.id"), index=True),
    )
    assert any("NOT NULL" in v for v in _violations_for("g_nullable"))


def test_wrong_type_organization_id(probe_table) -> None:
    probe_table(
        "g_wrongtype",
        Column("id", Integer, primary_key=True),
        Column(
            "organization_id",
            String(36),
            ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
    )
    assert any("must be a UUID" in v for v in _violations_for("g_wrongtype"))


def test_missing_fk(probe_table) -> None:
    probe_table(
        "g_nofk",
        Column("id", Integer, primary_key=True),
        Column("organization_id", PgUUID(as_uuid=True), nullable=False, index=True),
    )
    assert any("must reference organizations" in v for v in _violations_for("g_nofk"))


def test_missing_index(probe_table) -> None:
    probe_table(
        "g_noindex",
        Column("id", Integer, primary_key=True),
        Column(
            "organization_id",
            PgUUID(as_uuid=True),
            ForeignKey("organizations.id"),
            nullable=False,
        ),
    )
    assert any("must be indexed" in v for v in _violations_for("g_noindex"))


def test_self_scoped_without_id(probe_table) -> None:
    probe_table("g_selfnoid", Column("name", String(5)), marker=TENANT_SELF)
    assert any("no id column" in v for v in _violations_for("g_selfnoid"))


def test_valid_tenant_owned_table_passes(probe_table) -> None:
    probe_table(
        "g_valid",
        Column("id", Integer, primary_key=True),
        Column(
            "organization_id",
            PgUUID(as_uuid=True),
            ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
    )
    assert _violations_for("g_valid") == []
