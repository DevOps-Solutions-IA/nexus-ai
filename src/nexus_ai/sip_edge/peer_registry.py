"""PostgreSQL peer revisions fence config snapshots inside route transactions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.domain.sip_edge.models import SipPeerProfileRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError
from nexus_ai.sip_edge.peers import PeerProfile
from nexus_ai.sip_edge.targets import authorize_control


async def require_peer_revision(session: AsyncSession, peer_id: UUID, revision: int) -> None:
    row = (
        await session.execute(
            select(SipPeerProfileRecord)
            .where(SipPeerProfileRecord.id == peer_id)
            .with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if row is None or not row.active or row.revision != revision:
        raise SipRouteDeniedError()


class PeerRegistry:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def register(
        self,
        actor_user_id: UUID,
        profile: PeerProfile,
        *,
        expected_revision: int,
        active: bool = True,
    ) -> int:
        profile = PeerProfile.model_validate(profile.model_dump())
        if (
            isinstance(expected_revision, bool)
            or not 0 <= expected_revision < 9_223_372_036_854_775_807
        ):
            raise SipRouteDeniedError()
        payload = profile.model_dump(mode="json")
        try:
            async with self._database.transaction() as session:
                await authorize_control(session, actor_user_id)
                row = (
                    await session.execute(
                        select(SipPeerProfileRecord)
                        .where(SipPeerProfileRecord.id == profile.peer_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    if expected_revision != 0:
                        raise SipConflictError()
                    row = SipPeerProfileRecord(
                        id=profile.peer_id, revision=1, profile=payload, active=active
                    )
                    session.add(row)
                elif (
                    row.revision == expected_revision + 1
                    and row.profile == payload
                    and row.active == active
                ):
                    return row.revision
                elif row.revision != expected_revision:
                    raise SipConflictError()
                else:
                    row.profile, row.active = payload, active
                    row.revision += 1
                await session.flush()
                return row.revision
        except IntegrityError:
            raise SipConflictError() from None

    async def snapshot(self, expected: PeerProfile) -> int:
        async with self._database.transaction() as session:
            row = await session.get(SipPeerProfileRecord, expected.peer_id)
            if row is None or not row.active or row.profile != expected.model_dump(mode="json"):
                raise SipRouteDeniedError()
            return row.revision
