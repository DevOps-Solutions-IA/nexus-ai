import datetime as dt
from uuid import UUID, uuid7

import pytest
from pydantic import ValidationError

from nexus_ai.audit.platform import contracts


def test_fixed_domains_are_deterministic_and_cover_all_shards():
    domains = {contracts.integrity_domain("sentinel", UUID(int=value)) for value in range(1024)}
    assert domains == {f"platform:v2:{value:02x}" for value in range(16)}
    assert contracts.integrity_domain("sentinel", UUID(int=1)) == contracts.integrity_domain(
        "sentinel", UUID(int=1)
    )
    with pytest.raises(ValueError):
        contracts.integrity_domain("unknown", UUID(int=1))


def test_registry_is_closed_and_describes_source_authority():
    specification = contracts.PRODUCERS["sentinel"]
    assert specification.source_role == "nexus_sentinel"
    assert specification.durability == "SAME_TRANSACTION"
    with pytest.raises(TypeError):
        contracts.PRODUCERS["forged"] = specification


@pytest.mark.parametrize("producer", ["unknown", "runtime", "admin"])
def test_unknown_producer_is_rejected(producer):
    with pytest.raises(ValidationError):
        contracts.PlatformAuditIntent(
            source_id=uuid7(),
            producer=producer,
            action="sentinel.proposal.recorded",
            target_type="proposal",
            target_id=uuid7(),
            actor=contracts.PlatformAuditActor(kind="SERVICE", service="sentinel-store"),
            occurred_at=dt.datetime.now(dt.UTC),
        )
