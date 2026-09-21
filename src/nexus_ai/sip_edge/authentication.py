"""Authenticate before tenant discovery, committing a durable single-use nonce."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from nexus_ai.core.errors import ConfigurationError
from nexus_ai.domain.sip_edge.models import SipEdgeAuthHeadRecord, SipEdgeReplayRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.errors import SipRouteDeniedError, SipRouteUnavailableError
from nexus_ai.sip_edge.security import EdgeCredential, VerifiedRequest, verify_request


class EdgeAuthenticator:
    def __init__(
        self, database: Database, credentials: tuple[EdgeCredential, ...], *, capacity: int = 4096
    ) -> None:
        if not 1 <= len(credentials) <= 128 or not 1 <= capacity <= 4096:
            raise ValueError("bounded edge credentials and nonce capacity required")
        self._credentials = {credential.edge_id: credential for credential in credentials}
        if len(self._credentials) != len(credentials):
            raise ValueError("duplicate edge credential")
        self._database = database
        self._capacity = capacity

    async def authenticate(
        self,
        *,
        edge_id: UUID,
        boot_id: UUID,
        method: str,
        path: str,
        body: bytes,
        timestamp: int,
        nonce: str,
        signature: str,
    ) -> VerifiedRequest:
        credential = self._credentials.get(edge_id)
        if credential is None:
            raise SipRouteDeniedError()
        try:
            async with self._database.transaction() as session:
                database_time = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                verified = verify_request(
                    credential,
                    boot_id=boot_id,
                    method=method,
                    path=path,
                    body=body,
                    timestamp=timestamp,
                    nonce=nonce,
                    signature=signature,
                    database_timestamp=int(database_time.timestamp()),
                )
                await session.execute(
                    text("SELECT set_config('nxs.sip_edge_id', :edge, true)"),
                    {"edge": str(edge_id)},
                )
                await session.execute(
                    insert(SipEdgeAuthHeadRecord).values(edge_id=edge_id).on_conflict_do_nothing()
                )
                await session.execute(
                    select(SipEdgeAuthHeadRecord)
                    .where(SipEdgeAuthHeadRecord.edge_id == edge_id)
                    .with_for_update()
                )
                database_time = (await session.execute(select(func.clock_timestamp()))).scalar_one()
                if abs(int(database_time.timestamp()) - timestamp) > 30:
                    raise SipRouteDeniedError()
                expired = (
                    select(SipEdgeReplayRecord.nonce_digest)
                    .where(
                        SipEdgeReplayRecord.edge_id == edge_id,
                        SipEdgeReplayRecord.expires_at < database_time,
                    )
                    .order_by(SipEdgeReplayRecord.expires_at, SipEdgeReplayRecord.nonce_digest)
                    .limit(100)
                )
                await session.execute(
                    delete(SipEdgeReplayRecord).where(
                        SipEdgeReplayRecord.edge_id == edge_id,
                        SipEdgeReplayRecord.nonce_digest.in_(expired),
                    )
                )
                existing = await session.get(SipEdgeReplayRecord, (edge_id, verified.nonce_digest))
                if existing is not None:
                    raise SipRouteDeniedError()
                count = (
                    await session.execute(
                        select(func.count()).select_from(
                            select(SipEdgeReplayRecord.nonce_digest)
                            .where(SipEdgeReplayRecord.edge_id == edge_id)
                            .limit(self._capacity)
                            .subquery()
                        )
                    )
                ).scalar_one()
                if count >= self._capacity:
                    raise SipRouteUnavailableError()
                session.add(
                    SipEdgeReplayRecord(
                        edge_id=edge_id,
                        boot_id=boot_id,
                        nonce_digest=verified.nonce_digest,
                        request_digest=verified.request_digest,
                        expires_at=datetime.fromtimestamp(timestamp + 31, UTC),
                    )
                )
                await session.flush()
            return verified
        except DBAPIError, DatabaseTimeoutError, ConfigurationError:
            raise SipRouteUnavailableError() from None
