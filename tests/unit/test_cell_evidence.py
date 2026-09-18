"""Evidence generation fails closed on skipped, missing or failed test results."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import nxs_p18_evidence as evidence


@pytest.mark.parametrize("status", ["FAIL", "SKIP"])
def test_matrix_requires_executed_pass(status: str) -> None:
    result = evidence.evaluate(
        {"AC01": ["test_one*", "test_two*"]}, {"test_one": "PASS", "test_two": status}
    )
    assert result["AC01"]["result"] == "FAIL"
    missing = evidence.evaluate({"AC01": ["absent"]}, {"test_one": "PASS"})
    assert missing["AC01"]["missing"] == ["absent"]
    assert missing["AC01"]["result"] == "FAIL"


def test_pytest_reports_preserve_teardown_failure(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(evidence, "RESULTS", {})
    monkeypatch.setattr(evidence, "RESULTS_PATH", tmp_path / "outcomes.json")
    for node, when, failed, skipped in [
        ("one", "setup", False, False),
        ("one", "call", False, False),
        ("one", "teardown", True, False),
        ("two", "setup", False, True),
        ("three", "call", False, False),
    ]:
        evidence.pytest_runtest_logreport(
            SimpleNamespace(nodeid=node, when=when, failed=failed, skipped=skipped)
        )
    evidence.pytest_sessionfinish(None, 1)
    assert json.loads(evidence.RESULTS_PATH.read_text()) == {
        "exit_code": 1,
        "tests": {"one": "FAIL", "two": "SKIP", "three": "PASS"},
    }


def test_source_binding_excludes_generated_evidence(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("first")
    (tmp_path / ".nxs/evidence").mkdir(parents=True)
    (tmp_path / ".nxs/evidence/result.json").write_text("{}")
    first = evidence.source_snapshot(
        tmp_path, ["source.py", "missing", ".nxs/evidence/result.json"]
    )
    assert list(first) == ["source.py"]
    (tmp_path / "source.py").write_text("changed")
    assert first != evidence.source_snapshot(tmp_path, ["source.py"])


@pytest.mark.parametrize("return_code,write_report", [(0, True), (1, True), (1, False)])
def test_generator_records_real_exit_and_missing_report(
    tmp_path: Path, monkeypatch: Any, return_code: int, write_report: bool
) -> None:
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    monkeypatch.setattr(evidence, "RESULTS_PATH", tmp_path / "test-results.json")
    monkeypatch.setattr(evidence, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(evidence, "CONCURRENCY", {"C01": ["node"]})
    monkeypatch.setattr(evidence, "ACCEPTANCE", {"AC01": ["node"]})
    monkeypatch.setattr(evidence, "git", lambda *args: "candidate")
    (tmp_path / "candidate").write_text("fixture")

    def fake_run(*args: Any) -> Any:
        if write_report:
            evidence.RESULTS_PATH.write_text(
                json.dumps({"exit_code": return_code, "tests": {"node": "PASS"}})
            )
        return SimpleNamespace(returncode=return_code, stdout="unit fixture", stderr="")

    monkeypatch.setattr(evidence, "run", fake_run)
    assert evidence.main() == (0 if return_code == 0 else 1)
    result = json.loads((evidence.EVIDENCE / "execution-criteria.json").read_text())
    assert result["result"] == ("PASS" if return_code == 0 else "FAIL")
    assert result["exit_code"] == return_code
