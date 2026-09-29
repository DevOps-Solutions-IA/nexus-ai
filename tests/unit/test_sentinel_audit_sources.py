from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from nexus_ai.sentinel.control import SentinelControl


@pytest.mark.anyio
async def test_control_preserves_platform_operator_for_kill_switch() -> None:
    identity = uuid4()
    authority = SimpleNamespace(require=AsyncMock(return_value=identity))
    store = SimpleNamespace(set_mutable_actions=AsyncMock(return_value=2))
    control = SentinelControl(authority, store, SimpleNamespace())
    assert await control.set_mutable_actions("verified-token", 1, enabled=True) == 2
    actor = store.set_mutable_actions.call_args.kwargs["actor"]
    assert actor.kind == "HUMAN"
    assert actor.user_id == identity
    assert not hasattr(actor, "organization_id")


@pytest.mark.anyio
async def test_control_preserves_platform_operator_for_execution() -> None:
    identity = uuid4()
    authority = SimpleNamespace(require=AsyncMock(return_value=identity))
    executor = SimpleNamespace(claim=AsyncMock(), dispatch=AsyncMock(return_value="SUCCEEDED"))
    control = SentinelControl(authority, SimpleNamespace(), executor)
    await control.execute("verified-token", uuid4())
    assert executor.claim.call_args.kwargs["actor"].user_id == identity
    assert executor.dispatch.call_args.kwargs["actor"].user_id == identity
