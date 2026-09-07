"""Tenant context and namespace primitives (NXS-TENANT-002, NXS-TENANT-005)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from nexus_ai.core.errors import TenantContextInvalidError, TenantContextRequiredError
from nexus_ai.core.tenancy import (
    TenantContext,
    TenantContextSource,
    current_tenant,
    require_tenant,
    tenant_log_fields,
    tenant_namespace,
    tenant_scope,
)


def _ctx(organization_id: uuid.UUID | None = None) -> TenantContext:
    return TenantContext(organization_id or uuid.uuid7(), TenantContextSource.SYSTEM_BOOTSTRAP)


def test_tenant_context_is_immutable() -> None:
    context = _ctx()
    with pytest.raises((AttributeError, TypeError)):
        context.organization_id = uuid.uuid7()  # type: ignore[misc]


def test_require_tenant_without_scope_raises() -> None:
    assert current_tenant() is None
    with pytest.raises(TenantContextRequiredError):
        require_tenant()


def test_tenant_scope_binds_and_clears() -> None:
    context = _ctx()
    with tenant_scope(context) as bound:
        assert bound is context
        assert current_tenant() is context
        assert require_tenant() is context
        assert tenant_log_fields()["organization_id"] == str(context.organization_id)
    assert current_tenant() is None
    assert tenant_log_fields() == {}


def test_portable_round_trip() -> None:
    context = TenantContext(uuid.uuid7(), TenantContextSource.TEST_HEADER, correlation_id="corr-1")
    restored = TenantContext.from_portable(context.to_portable())
    assert restored == context


def test_portable_rejects_malformed() -> None:
    with pytest.raises(TenantContextInvalidError):
        TenantContext.from_portable({"organization_id": "not-a-uuid", "source": "x"})


def test_context_never_bleeds_between_tasks() -> None:
    async def worker(index: int) -> str:
        context = TenantContext(uuid.uuid7(), TenantContextSource.SYSTEM_BOOTSTRAP)
        with tenant_scope(context):
            await asyncio.sleep(0)
            observed = require_tenant()
            assert observed is context
            return str(observed.organization_id)

    async def main() -> None:
        results = await asyncio.gather(*(worker(i) for i in range(100)))
        assert len(set(results)) == 100
        assert current_tenant() is None

    asyncio.run(main())


class TestNamespace:
    def test_deterministic_and_scoped(self) -> None:
        org_a, org_b = uuid.uuid7(), uuid.uuid7()
        assert tenant_namespace(org_a, "cache", "session") == f"nxs:{org_a}:cache:session"
        assert tenant_namespace(org_a, "x") != tenant_namespace(org_b, "x")

    def test_rejects_missing_or_unsafe(self) -> None:
        org = uuid.uuid7()
        with pytest.raises(TenantContextInvalidError):
            tenant_namespace(org)
        with pytest.raises(TenantContextInvalidError):
            tenant_namespace(org, "bad segment")
        with pytest.raises(TenantContextInvalidError):
            tenant_namespace(org, "x" * 200)
        with pytest.raises(TenantContextInvalidError):
            tenant_namespace("not-a-uuid", "x")  # type: ignore[arg-type]
