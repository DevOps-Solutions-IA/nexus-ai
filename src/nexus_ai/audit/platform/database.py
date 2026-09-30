"""Role-checked platform connection, never added to tenant request state."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus_ai.audit.contracts import AuditProvenanceError
from nexus_ai.audit.platform.config import PlatformAuditSettings


class PlatformAuditDatabase:
    def __init__(self, settings: PlatformAuditSettings) -> None:
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_dsn.get_secret_value().replace(
                "postgresql://", "postgresql+asyncpg://", 1
            ),
            pool_size=settings.pool_size,
            max_overflow=0,
            pool_timeout=settings.database_timeout_seconds,
            hide_parameters=True,
            connect_args={
                "timeout": settings.database_timeout_seconds,
                "command_timeout": settings.database_timeout_seconds,
            },
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self, *, snapshot: bool = False) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            if snapshot:
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                )
            row = (
                await session.execute(
                    text(
                        "SELECT current_user, session_user, rolsuper, rolbypassrls, rolcreaterole, "
                        "rolcreatedb, rolreplication, "
                        "has_schema_privilege(current_user,'public','CREATE'), "
                        "coalesce(current_setting('nxs.organization_id',true), ''), "
                        "EXISTS (SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) "
                        "FROM pg_roles WHERE rolname=current_user"
                    )
                )
            ).one()
            if (
                row[0] != self.settings.expected_role
                or row[1] != self.settings.expected_role
                or any(row[2:8])
                or row[8]
                or row[9]
            ):
                raise AuditProvenanceError("unsafe platform database identity")
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
