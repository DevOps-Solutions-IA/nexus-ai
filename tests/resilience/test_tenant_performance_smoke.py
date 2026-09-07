"""Lightweight tenant-isolation performance smoke (section 73).

Not capacity certification (that is P28). Detects pathological regressions in tenant
context binding, RLS lookup and Organization repository access.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from nexus_ai.core.tenancy import TenantContext, TenantContextSource

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_scoped_reads_have_sane_baseline(
    organization_service: Any, make_organization, capsys: pytest.CaptureFixture[str]
) -> None:
    orgs = [await make_organization(activate=True) for _ in range(5)]
    contexts = [TenantContext(o.id, TenantContextSource.SYSTEM_BOOTSTRAP) for o in orgs]

    iterations = 200
    started = time.perf_counter()
    for index in range(iterations):
        current = await organization_service.get_current(contexts[index % len(contexts)])
        assert current.id == contexts[index % len(contexts)].organization_id
    elapsed = time.perf_counter() - started

    per_op_ms = elapsed / iterations * 1000
    # Generous ceiling for a shared runner: a scoped read + RLS lookup should be well under this.
    assert per_op_ms < 40, f"tenant read baseline {per_op_ms:.2f} ms/op"
    capsys.readouterr()
    print(f"tenant-perf-smoke: {iterations} scoped reads, {per_op_ms:.2f} ms/op")
