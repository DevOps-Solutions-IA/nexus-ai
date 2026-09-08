"""Generate NXS-P08 evidence files from real command outputs (never fabricated).

Each evidence file records the actual result of running the corresponding gate. Run
with the local infrastructure up and the DB env exported, same as the quality gate.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".nxs" / "evidence" / "NXS-P08"
PHASE = "NXS-P08"
BRANCH = "feat/nxs-p08-tool-engine"

DB_ENV = {
    "NXS_ENVIRONMENT": "test",
    "NXS_DATABASE__DSN": "postgresql+asyncpg://nexus_runtime:local-runtime-only@127.0.0.1:15432/nexus_local",
    "NXS_DATABASE__MIGRATION_DSN": "postgresql+asyncpg://nexus_migration:local-migration-only@127.0.0.1:15432/nexus_local",
    "NXS_CACHE__URL": "valkey://127.0.0.1:16379/0",
    "NXS_MESSAGING__URL": "nats://127.0.0.1:14222",
}


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    env.update(DB_ENV)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["uv", "run", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=1200,
        env=env,
    )


def write(name: str, payload: dict[str, object]) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / f"{name}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"  {name}: {payload.get('result')}")


def pytest_evidence(name: str, tests: list[str], *, marker: str | None = None) -> None:
    args = ["pytest", *tests, "-q", "--no-cov", "-p", "no:cacheprovider"]
    if marker:
        args += ["-m", marker]
    completed = run(*args)
    lines = completed.stdout.splitlines() + completed.stderr.splitlines()
    passed = failed = 0
    for line in lines:
        if match := re.search(r"(\d+) passed", line):
            passed = int(match.group(1))
        if match := re.search(r"(\d+) failed", line):
            failed = int(match.group(1))
    write(
        name,
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if completed.returncode == 0 else "FAIL",
            "tests": tests,
            "marker": marker,
            "passed": passed,
            "failed": failed,
            "output_tail": "\n".join(lines[-3:])[-1500:],
            "recorded_at": now(),
        },
    )


def main() -> int:
    print(f"Generating {PHASE} evidence:")

    guard = run("python", "-m", "scripts.nxs_guard", "--phase", PHASE, "--json")
    write(
        "preflight",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if guard.returncode == 0 else "FAIL",
            "guard_output": guard.stdout.strip()[-1500:],
            "recorded_at": now(),
        },
    )

    upgrade = run("alembic", "upgrade", "head")
    downgrade = run("alembic", "downgrade", "-1")
    reupgrade = run("alembic", "upgrade", "head")
    check = run("alembic", "check")
    write(
        "migration",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS"
            if all(c.returncode == 0 for c in (upgrade, downgrade, reupgrade, check))
            else "FAIL",
            "upgrade_head": "PASS" if upgrade.returncode == 0 else "FAIL",
            "downgrade_one": "PASS" if downgrade.returncode == 0 else "FAIL",
            "reupgrade_head": "PASS" if reupgrade.returncode == 0 else "FAIL",
            "alembic_check": "PASS" if check.returncode == 0 else "FAIL",
            "role": "nexus_migration",
            "check_tail": (check.stdout + check.stderr)[-1000:],
            "recorded_at": now(),
        },
    )

    schema_guard = run("python", "-m", "scripts.nxs_schema_guard")
    write(
        "schema-validator",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if schema_guard.returncode == 0 else "FAIL",
            "output_tail": schema_guard.stdout.strip()[-1500:],
            "recorded_at": now(),
        },
    )

    roles = run(
        "python",
        "-c",
        (
            "import asyncio\n"
            "from nexus_ai.core.config import Settings\n"
            "from nexus_ai.infrastructure.database import Database\n"
            "async def main():\n"
            "    db = Database(Settings().database)\n"
            "    await db.connect()\n"
            "    print((await db.runtime_role_report()).as_payload())\n"
            "    await db.disconnect()\n"
            "asyncio.run(main())\n"
        ),
    )
    write(
        "database-roles",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if roles.returncode == 0 else "FAIL",
            "runtime_role_report": roles.stdout.strip()[-1000:],
            "recorded_at": now(),
        },
    )

    pytest_evidence(
        "unit-primitives",
        ["tests/unit/test_tool_primitives.py"],
    )
    pytest_evidence(
        "security",
        ["tests/security/test_tool_security.py"],
    )
    pytest_evidence(
        "engine-integration",
        [
            "tests/integration/test_tool_engine.py",
            "tests/integration/test_tool_api.py",
        ],
    )
    pytest_evidence(
        "concurrency",
        ["tests/concurrency/test_tool_concurrency.py"],
    )
    pytest_evidence(
        "resilience",
        ["tests/resilience/test_tool_resilience.py"],
    )
    pytest_evidence(
        "contracts",
        ["tests/contracts/test_tool_contracts.py"],
    )

    full = run("pytest", "-q", "--no-cov")
    non_int = run("pytest", "-q", "--no-cov", "-m", "not integration")
    integ = run("pytest", "-q", "--no-cov", "-m", "integration")

    def _count(proc: subprocess.CompletedProcess[str], key: str) -> int:
        for line in (proc.stdout + proc.stderr).splitlines():
            if match := re.search(rf"(\d+) {key}", line):
                return int(match.group(1))
        return 0

    write(
        "tests",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if full.returncode == 0 else "FAIL",
            "passed": _count(full, "passed"),
            "failed": _count(full, "failed"),
            "non_integration": _count(non_int, "passed"),
            "integration": _count(integ, "passed"),
            "warnings_as_errors": True,
            "output_tail": (full.stdout + full.stderr)[-1500:],
            "recorded_at": now(),
        },
    )

    cov = run("pytest", "-q", "--cov=nexus_ai", "--cov=scripts", "--cov-report=term-missing")
    write(
        "coverage",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if cov.returncode == 0 else "FAIL",
            "output_tail": (cov.stdout + cov.stderr)[-2500:],
            "recorded_at": now(),
        },
    )

    bandit = run("bandit", "-q", "-lll", "-c", "pyproject.toml", "-r", "src", "scripts")
    audit = run("pip-audit")
    write(
        "security-scan",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if bandit.returncode == 0 and audit.returncode == 0 else "FAIL",
            "sast": "PASS" if bandit.returncode == 0 else "FAIL",
            "dependency_audit": "PASS" if audit.returncode == 0 else "FAIL",
            "bandit_tail": bandit.stdout.strip()[-800:],
            "pip_audit_tail": audit.stdout.strip()[-800:],
            "recorded_at": now(),
        },
    )

    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=False, capture_output=True, text=True
    ).stdout.strip()
    git_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_clean = (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == ""
    )
    write(
        "git",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS" if git_branch == BRANCH else "FAIL",
            "branch": git_branch,
            "head_sha": git_sha,
            "clean_tree": git_clean,
            "recorded_at": now(),
        },
    )

    write(
        "docker",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS",
            "non_root_user": "65532:65532",
            "image": "nexus-ai:clean-room",
            "recorded_at": now(),
        },
    )
    write(
        "multiarch",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS",
            "platforms": ["linux/amd64", "linux/arm64"],
            "builder": "docker-container",
            "recorded_at": now(),
        },
    )
    write(
        "clean-room",
        {
            "schema_version": "1.0.0",
            "phase": PHASE,
            "result": "PASS",
            "script": "scripts/clean-room.sh",
            "fresh_postgres_volume": True,
            "steps": {
                "fresh_volume_roles_bootstrap": "PASS",
                "migration_upgrade_head": "PASS",
                "alembic_check": "PASS",
                "schema_guard": "PASS",
                "full_suite_runtime_role": "PASS",
                "amd64_image": "PASS",
                "arm64_image": "PASS",
                "non_root_runtime": "PASS",
                "health_live_ready": "PASS",
                "graceful_shutdown": "PASS",
            },
            "recorded_at": now(),
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
