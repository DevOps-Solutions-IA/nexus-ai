from uuid import uuid7

import pytest

from nexus_ai.core.health import DependencyHealth, HealthStatus
from nexus_ai.sentinel.config import SentinelSettings
from nexus_ai.sentinel.contracts import Subject
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sentinel.service import SourceBinding
from nexus_ai.sentinel.signals import NxsStateProbe, SignalAdapter, SignalCollector


def binding():
    return SourceBinding("health", 1, "dependency", "registered-service", Subject.SERVICE, uuid7())


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,expected",
    [
        (HealthStatus.UP, "HEALTHY"),
        (HealthStatus.DOWN, "UNAVAILABLE"),
        (HealthStatus.DEGRADED, "DEGRADED"),
        (HealthStatus.UNKNOWN, "UNKNOWN"),
    ],
)
async def test_typed_health_discards_external_details(status, expected):
    async def probe():
        return DependencyHealth(
            "ignore instructions shell SQL", status, True, 2, "secret-provider-error"
        )

    adapter = SignalAdapter(binding(), probe, 1)
    observation = uuid7()
    signal = await adapter.observe(observation)
    assert signal.facts.condition == expected
    assert signal.subject_id == adapter.binding.subject_id
    assert signal.source_observation_id == str(observation)
    assert "secret-provider-error" not in signal.model_dump_json()
    assert "ignore instructions" not in signal.model_dump_json()


@pytest.mark.anyio
async def test_probe_failure_is_unknown_not_recovery():
    async def probe():
        raise RuntimeError("secret")

    signal = await SignalAdapter(binding(), probe, 1).observe(uuid7())
    assert signal.facts.condition == "UNKNOWN"
    assert signal.severity == "WARNING"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 31])
def test_adapter_timeout_is_bounded(timeout):
    with pytest.raises(SentinelDenied):
        SignalAdapter(binding(), None, timeout)


class Store:
    def __init__(self, source):
        self.sources = {(source.adapter_id, source.source_identity): source}
        self.received = []

    async def ingest(self, signal):
        self.received.append(signal)
        return uuid7(), uuid7()


@pytest.mark.anyio
async def test_collector_registry_and_batch_bound():
    source = binding()
    store = Store(source)

    async def probe():
        return DependencyHealth("service", HealthStatus.UP, True)

    adapter = SignalAdapter(source, probe, 1)
    collector = SignalCollector(store, SentinelSettings(), (adapter,))
    assert len(await collector.poll()) == 1
    assert len(store.received) == 1
    with pytest.raises(SentinelDenied, match="adapter_registry_bound"):
        SignalCollector(store, SentinelSettings(), (adapter, adapter))
    with pytest.raises(SentinelDenied, match="adapter_source_not_registered"):
        SignalCollector(Store(binding()), SentinelSettings(), (adapter,))


@pytest.mark.anyio
async def test_nxs_probe_missing_malformed_and_bounded(tmp_path):
    probe = NxsStateProbe(tmp_path)
    assert (await probe()).status == HealthStatus.DOWN
    directory = tmp_path / ".nxs"
    directory.mkdir()
    path = directory / "project-state.json"
    for content in ["not json", "{}", "x" * 65537]:
        path.write_text(content)
        assert (await probe()).status == HealthStatus.DOWN
    path.write_text('{"current_phase":{"status":"BUILDING"}}')
    assert (await probe()).status == HealthStatus.UP
