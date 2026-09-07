"""Async PostgreSQL connectivity foundation (NXS-DATA-001).

Provides a pooled async engine, a session factory, safe transaction primitives and a
health probe. No engine or connection is created at import time. Business tables are
out of scope for P01 — this is only the reliable persistence boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus_ai.core.config import DatabaseSettings
from nexus_ai.core.errors import ConfigurationError, DependencyUnavailableError
from nexus_ai.core.health import DependencyHealth, HealthStatus, timed_probe


class Database:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
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
        """An explicit unit of work: commit on success, rollback on any exception."""
        async with self.session() as session, session.begin():
            yield session

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
