from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from scripts.nxs_control.core import ControlError, git, iso_now, load_json, utc_now, write_json


def acquire(root: Path, phase: str, actor: str) -> None:
    path = root / ".nxs/execution-lock.json"
    lock = load_json(path)
    if lock["state"] == "ACTIVE":
        raise ControlError(f"lock already active for {lock['phase']}")
    policy = load_json(root / ".nxs/execution-policy.json")
    now = utc_now()
    lock.update(
        {
            "state": "ACTIVE",
            "phase": phase,
            "branch": git(root, "branch", "--show-current"),
            "actor": actor,
            "started_at": iso_now(),
            "expires_at": (now + timedelta(seconds=policy["lock"]["ttl_seconds"]))
            .isoformat()
            .replace("+00:00", "Z"),
            "repository_commit": git(root, "rev-parse", "HEAD"),
        }
    )
    write_json(path, lock)


def release(root: Path, phase: str) -> None:
    path = root / ".nxs/execution-lock.json"
    lock = load_json(path)
    if lock["state"] == "ACTIVE" and lock["phase"] != phase:
        raise ControlError(f"lock belongs to {lock['phase']}")
    write_json(
        path,
        {
            "schema_version": "1.0.0",
            "state": "RELEASED",
            "phase": None,
            "branch": None,
            "actor": None,
            "started_at": None,
            "expires_at": None,
            "repository_commit": None,
        },
    )


def recover(root: Path) -> None:
    path = root / ".nxs/execution-lock.json"
    lock = load_json(path)
    if lock["state"] != "ACTIVE":
        raise ControlError("no active lock")
    from datetime import datetime

    expires_at = datetime.fromisoformat(lock["expires_at"].replace("Z", "+00:00"))
    if expires_at > utc_now():
        raise ControlError("active lock has not expired")
    if git(root, "status", "--porcelain"):
        raise ControlError("stale lock recovery requires a clean working tree")
    release(root, lock["phase"])
