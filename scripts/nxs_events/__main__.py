"""``python -m scripts.nxs_events`` — inspect and replay dead-lettered events.

Replay is deliberately manual and audited: an operator names a dead letter, the tool
re-arms it (an ``OUTBOX_PUBLISH`` failure resets the outbox row to ``PENDING``; a
``CONSUMER`` failure clears the consumer receipt and re-publishes the event), and stamps
``replayed_at``. Nothing replays automatically and nothing replays forever.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import text

from nexus_ai.core.config import Settings
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.event_dead_letter import DeadLetterRepository
from nexus_ai.infrastructure.jetstream import JetStreamTransport
from nexus_ai.infrastructure.messaging import Messaging


async def _list(limit: int) -> int:
    settings = Settings()
    database = Database(settings.database)
    await database.connect()
    try:
        repo = DeadLetterRepository()
        async with database.transaction() as session:
            rows = await repo.list_for_replay(session, limit=limit)
            payload = [
                {
                    "id": str(row.id),
                    "event_id": str(row.event_id),
                    "event_type": row.event_type,
                    "origin": row.origin,
                    "failure_class": row.failure_class,
                    "error_code": row.error_code,
                    "consumer_name": row.consumer_name,
                    "attempt_count": row.attempt_count,
                    "recorded_at": row.recorded_at.isoformat(),
                }
                for row in rows
            ]
    finally:
        await database.disconnect()
    print(json.dumps({"pending_dead_letters": payload}, indent=2))
    return 0


async def _replay(dead_letter_id: str) -> int:
    settings = Settings()
    database = Database(settings.database)
    messaging = Messaging(settings.messaging)
    await database.connect()
    await messaging.connect()
    transport = JetStreamTransport(
        messaging, settings.events, environment=settings.environment.value
    )
    repo = DeadLetterRepository()
    try:
        async with database.transaction() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM event_dead_letters WHERE id = :i AND replayed_at IS NULL"
                        ),
                        {"i": dead_letter_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                print("NXS EVENTS REPLAY\nRESULT: BLOCK")
                print(f"REASON: no pending dead letter {dead_letter_id}")
                return 2
            envelope = EventEnvelope.model_validate(row["envelope"])
            subject = envelope.subject(
                prefix=settings.events.subject_prefix,
                environment=settings.environment.value,
            ).value
            if row["origin"] == "OUTBOX_PUBLISH":
                await session.execute(
                    text(
                        "UPDATE event_outbox SET status='PENDING', attempt_count=0, "
                        "available_at=now(), last_error_code=NULL, lease_owner=NULL, "
                        "lease_expires_at=NULL WHERE id = :i"
                    ),
                    {"i": envelope.event_id},
                )
            else:
                # Re-arm the receipt (the runtime role has no DELETE): a PROCESSING
                # receipt is re-eligible, so the next delivery reprocesses the event.
                await session.execute(
                    text(
                        "UPDATE consumer_receipts SET status='PROCESSING', "
                        "last_error_code=NULL, updated_at=now() "
                        "WHERE event_id = :e AND consumer_name = :c"
                    ),
                    {"e": envelope.event_id, "c": row["consumer_name"]},
                )
                await transport.publish(
                    subject,
                    envelope.to_json(),
                    headers={**envelope.nats_headers(), "Nxs-Replay": "true"},
                )
            await repo.mark_replayed(session, row["id"])
    finally:
        await messaging.disconnect()
        await database.disconnect()
    print("NXS EVENTS REPLAY\nRESULT: PASS")
    print(f"DEAD_LETTER: {dead_letter_id}\nEVENT_ID: {envelope.event_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="nxs_events")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("dead-letters", help="list pending dead letters")
    listing.add_argument("--limit", type=int, default=50)
    replay = sub.add_parser("replay", help="replay one dead letter")
    replay.add_argument("--id", required=True)
    replay.add_argument("--confirm", action="store_true", required=True)
    arguments = parser.parse_args()
    if arguments.command == "dead-letters":
        return asyncio.run(_list(arguments.limit))
    return asyncio.run(_replay(arguments.id))


if __name__ == "__main__":
    sys.exit(main())
