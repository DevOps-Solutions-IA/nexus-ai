"""Certification refuses unexecuted, skipped or failed proof nodes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.nxs_p19_evidence as evidence


def test_every_governance_identifier_has_explicit_test_patterns() -> None:
    assert set(evidence.CONCURRENCY) == {f"C{index:02}" for index in range(1, 33)}
    assert set(evidence.ACCEPTANCE) == {f"AC{index:02}" for index in range(1, 44)}
    assert set(evidence.TRANSPORT) == {f"T{index:02}" for index in range(1, 11)}
    for mapping in (evidence.CONCURRENCY, evidence.ACCEPTANCE, evidence.TRANSPORT):
        assert all(patterns for patterns in mapping.values())
        assert all(item["result"] == "FAIL" for item in evidence.evaluate(mapping, {}).values())


def test_failed_teardown_cannot_be_overwritten_by_a_passing_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "RESULTS", {})
    monkeypatch.setattr(evidence, "RUNTIME", tmp_path)
    for failed, skipped, when in (
        (False, False, "call"),
        (True, False, "teardown"),
        (False, False, "call"),
    ):
        evidence.pytest_runtest_logreport(
            SimpleNamespace(
                nodeid="tests/proof.py::test_proof",
                failed=failed,
                skipped=skipped,
                when=when,
            )
        )
    evidence.pytest_runtest_logreport(
        SimpleNamespace(
            nodeid="tests/proof.py::test_skipped",
            failed=False,
            skipped=True,
            when="setup",
        )
    )
    evidence.pytest_sessionfinish(None, 1)
    results = json.loads((tmp_path / "p19-test-results.json").read_text())
    assert results["exit_code"] == 1
    assert results["tests"] == {
        "tests/proof.py::test_proof": "FAIL",
        "tests/proof.py::test_skipped": "SKIP",
    }
    assert (
        evidence.evaluate({"C01": ["tests/proof.py::*"]}, results["tests"])["C01"]["result"]
        == "FAIL"
    )
