import json
from uuid import uuid7

import asyncpg
import pytest
from sqlalchemy import select

from nexus_ai.audit.platform.contracts import PlatformAuditActor
from nexus_ai.domain.sentinel.models import SentinelControlState
from nexus_ai.sentinel.contracts import Risk
from tests.conftest import SUPERUSER_DSN
from tests.integration.test_sentinel_execution import pipeline
from tests.integration.test_sentinel_persistence import database as database
from tests.integration.test_sentinel_persistence import store

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_kill_switch_and_audit_intent_share_rollback(database, monkeypatch):
    service = store(database, [])

    async def fail(*args, **kwargs):
        raise RuntimeError("injected audit insertion failure")

    monkeypatch.setattr("nexus_ai.sentinel.service.emit_platform_audit", fail)
    with pytest.raises(RuntimeError, match="injected audit"):
        await service.set_mutable_actions(
            1, enabled=True, actor=PlatformAuditActor(kind="HUMAN", user_id=uuid7())
        )
    async with database.transaction() as session:
        control = await session.scalar(select(SentinelControlState))
        assert control.revision == 1
        assert not control.mutable_actions_enabled


async def test_real_execution_and_approval_keep_platform_provenance(database):
    executor, _adapter, identity, _request, *_rest = await pipeline(database, Risk.REVERSIBLE)
    operator = uuid7()
    actor = PlatformAuditActor(kind="HUMAN", user_id=operator)
    claim = await executor.claim(identity, uuid7(), actor=actor)
    assert await executor.dispatch(claim, actor=actor) == "SUCCEEDED"
    admin = await asyncpg.connect(SUPERUSER_DSN)
    try:
        rows = await admin.fetch(
            "SELECT payload FROM platform_audit_intents "
            "WHERE payload->>'target_id' = $1 OR payload->'metadata'->>'related_id' = $2",
            str(claim.execution_id),
            str(identity),
        )
    finally:
        await admin.close()
    facts = [json.loads(row["payload"]) for row in rows]
    assert "sentinel.approval.recorded" in {fact["action"] for fact in facts}
    executions = [fact for fact in facts if fact["action"].startswith("sentinel.execution.")]
    assert {fact["action"] for fact in executions} == {
        "sentinel.execution.claimed",
        "sentinel.execution.dispatched",
        "sentinel.execution.completed",
    }
    for fact in executions:
        assert fact["scope"] == "PLATFORM"
        assert fact["organization_id"] is None
        assert fact["actor"]["kind"] == "HUMAN"
        assert fact["actor"]["user_id"] == str(operator)
