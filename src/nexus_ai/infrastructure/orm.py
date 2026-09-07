"""Declarative ORM base, naming convention and the tenant-owned model primitive.

``TenantOwnedMixin`` is the reusable contract for every future tenant-scoped table
(NXS-TENANT-003): a non-nullable ``organization_id`` UUID foreign key to
``organizations.id`` plus a ``tenant_scoped`` table marker the schema guard and RLS
tooling rely on. The ``organizations`` table itself is self-scoped (RLS on ``id``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, MetaData
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

TENANT_SCOPED_KEY = "tenant_scoped"
TENANT_SELF = "self"
TENANT_OWNED = "owned"


class Base(DeclarativeBase):
    metadata = metadata


class TenantOwnedMixin:
    """Adds the mandatory tenant column + marker to a future tenant-owned model."""

    __abstract__ = True

    @declared_attr.directive
    def __table_args__(cls) -> dict[str, object]:
        return {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}}

    @declared_attr
    def organization_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            PgUUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )
