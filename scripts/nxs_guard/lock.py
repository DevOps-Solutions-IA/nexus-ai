from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.nxs_control.core import ControlError, git, iso_now, load_json, utc_now, write_json

RELEASED_LOCK: dict[str, Any] = {
    "schema_version": "1.0.0",
    "state": "RELEASED",
    "phase": None,
    "branch": None,
    "actor": None,
    "started_at": None,
    "expires_at": None,
    "repository_commit": None,
}


def _expires_at(lock: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(lock["expires_at"]).replace("Z", "+00:00"))


def is_stale(lock: dict[str, Any]) -> bool:
    return lock["state"] == "ACTIVE" and _expires_at(lock) <= utc_now()


def acquire(root: Path, phase: str, actor: str) -> dict[str, Any]:
    path = root / ".nxs/execution-lock.json"
    lock = load_json(path)
    if lock["state"] == "ACTIVE":
        if lock["phase"] == phase and lock["actor"] == actor and not is_stale(lock):
            return lock
        raise ControlError(f"lock already active for {lock['phase']}")
    if not actor.strip():
        raise ControlError("an actor is required to acquire the execution lock")
    policy = load_json(root / ".nxs/execution-policy.json")
    now = utc_now()
    lock = {
        "schema_version": "1.0.0",
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
    write_json(path, lock)
    return lock


def release(root: Path, phase: str) -> None:
    path = root / ".nxs/execution-lock.json"
    lock = load_json(path)
    if lock["state"] == "ACTIVE" and lock["phase"] != phase:
        raise ControlError(f"lock belongs to {lock['phase']}")
    write_json(path, dict(RELEASED_LOCK))


def release_if_held(root: Path, phase: str) -> bool:
    """Release the lock only when this phase holds it. Safe to call unconditionally."""
    lock = load_json(root / ".nxs/execution-lock.json")
    if lock["state"] == "ACTIVE" and lock["phase"] == phase:
        release(root, phase)
        return True
    return False


def recover(root: Path) -> None:
    path = root / ".nxs/execution-lock.json"
    lock = load_json(path)
    if lock["state"] != "ACTIVE":
        raise ControlError("no active lock")
    if _expires_at(lock) > utc_now():
        raise ControlError("active lock has not expired")
    if git(root, "status", "--porcelain"):
        raise ControlError("stale lock recovery requires a clean working tree")
    release(root, str(lock["phase"]))


def status(root: Path) -> dict[str, Any]:
    lock = load_json(root / ".nxs/execution-lock.json")
    result: dict[str, Any] = dict(lock)
    if lock["state"] == "ACTIVE":
        remaining = (_expires_at(lock) - utc_now()).total_seconds()
        result["stale"] = remaining <= 0
        result["seconds_remaining"] = max(0, int(remaining))
    else:
        result["stale"] = False
        result["seconds_remaining"] = 0
    return result
