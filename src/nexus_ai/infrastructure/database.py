"""Async PostgreSQL connectivity foundation (NXS-DATA-001).

Provides a pooled async engine, a session factory, safe transaction primitives and a
health probe. No engine or connection is created at import time. Business tables are
out of scope for P01 — this is only the reliable persistence boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus_ai.core.config import DatabaseSettings
from nexus_ai.core.errors import (
    ConfigurationError,
    DependencyUnavailableError,
    TenantContextInvalidError,
)
from nexus_ai.core.health import DependencyHealth, HealthStatus, timed_probe
from nexus_ai.infrastructure.tenant_session import TenantSession

DEFAULT_CONTEXT_SETTING = "nxs.organization_id"


@dataclass(frozen=True, slots=True)
class RuntimeRoleReport:
    role: str
    is_superuser: bool
    can_bypass_rls: bool
    can_create_role: bool
    can_create_db: bool

    @property
    def can_bypass_tenancy(self) -> bool:
        return self.is_superuser or self.can_bypass_rls

    def as_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "is_superuser": self.is_superuser,
            "can_bypass_rls": self.can_bypass_rls,
            "can_create_role": self.can_create_role,
            "can_create_db": self.can_create_db,
            "can_bypass_tenancy": self.can_bypass_tenancy,
        }


class Database:
    def __init__(
        self, settings: DatabaseSettings, *, context_setting: str = DEFAULT_CONTEXT_SETTING
    ) -> None:
        self._settings = settings
        self._context_setting = context_setting
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @property
    def is_connected(self) -> bool:
        return self._engine is not None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise ConfigurationError("database engine is not initialised")
        return self._engine

    async def connect(self) -> None:
        if self._engine is not None:
            return
        if self._settings.dsn is None:
            raise ConfigurationError("NXS_DATABASE__DSN is not configured")
        self._engine = create_async_engine(
            self._settings.async_dsn(),
            pool_size=self._settings.pool_size,
            max_overflow=self._settings.max_overflow,
            pool_timeout=self._settings.pool_timeout_seconds,
            pool_recycle=self._settings.pool_recycle_seconds,
            pool_pre_ping=True,
            connect_args={
                "timeout": self._settings.connect_timeout_seconds,
                "command_timeout": self._settings.command_timeout_seconds,
                "server_settings": {"application_name": "nexus-ai-backend"},
            },
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, autoflush=False
        )

    async def disconnect(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session with no implicit transaction. Rolls back on error, always closes."""
        if self._sessionmaker is None:
            raise ConfigurationError("database is not connected")
        session = self._sessionmaker()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A SYSTEM unit of work: commit on success, rollback on any exception.

        This is NOT tenant-scoped. Tenant code paths must use ``tenant_transaction``.
        """
        async with self.session() as session, session.begin():
            yield session

    @asynccontextmanager
    async def tenant_transaction(self, organization_id: UUID) -> AsyncIterator[TenantSession]:
        """A tenant unit of work: binds the transaction-local tenant GUC (NXS-TENANT-004).

        ``set_config(name, value, is_local => true)`` scopes the setting to this
        transaction only, so a pooled connection returned to the pool carries no tenant
        context into the next transaction. On commit or rollback the scope disappears.
        """
        if not isinstance(organization_id, UUID):
            raise TenantContextInvalidError("tenant_transaction requires a UUID organization_id")
        async with self.session() as session, session.begin():
            await session.execute(
                text("SELECT set_config(:name, :value, true)"),
                {"name": self._context_setting, "value": str(organization_id)},
            )
            bound = (
                await session.execute(
                    text("SELECT current_setting(:name, true)"),
                    {"name": self._context_setting},
                )
            ).scalar_one_or_none()
            if bound != str(organization_id):
                raise TenantContextInvalidError("failed to bind transaction-local tenant context")
            yield TenantSession(organization_id=organization_id, session=session)

    async def runtime_role_report(self) -> RuntimeRoleReport:
        """Inspect the connected role's ability to bypass tenancy (NXS-SEC-003)."""
        async with self.session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
        return RuntimeRoleReport(
            role=str(row[0]),
            is_superuser=bool(row[1]),
            can_bypass_rls=bool(row[2]),
            can_create_role=bool(row[3]),
            can_create_db=bool(row[4]),
        )

    async def probe(self, *, timeout: float) -> DependencyHealth:
        if self._engine is None:
            return DependencyHealth(
                "postgresql", HealthStatus.DOWN, self._settings.required, None, "not_connected"
            )

        async def check() -> None:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        return await timed_probe("postgresql", self._settings.required, check, timeout=timeout)

    async def require_ready(self) -> None:
        health = await self.probe(timeout=self._settings.connect_timeout_seconds)
        if health.status is not HealthStatus.UP:
            raise DependencyUnavailableError("The database is not available.")
