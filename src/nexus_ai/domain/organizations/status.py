"""Organization lifecycle state machine (NXS-ORG-003).

PROVISIONING → ACTIVE | ARCHIVED
ACTIVE       → SUSPENDED | ARCHIVED
SUSPENDED    → ACTIVE | ARCHIVED
ARCHIVED     → (terminal)

Only ACTIVE permits normal tenant traffic. PROVISIONING is reserved for the P05
provisioner. Invalid transitions raise a stable NXS error.
"""

from __future__ import annotations

from enum import StrEnum

from nexus_ai.core.errors import OrganizationInvalidStateError


class OrganizationStatus(StrEnum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


_TRANSITIONS: dict[OrganizationStatus, frozenset[OrganizationStatus]] = {
    OrganizationStatus.PROVISIONING: frozenset(
        {OrganizationStatus.ACTIVE, OrganizationStatus.ARCHIVED}
    ),
    OrganizationStatus.ACTIVE: frozenset(
        {OrganizationStatus.SUSPENDED, OrganizationStatus.ARCHIVED}
    ),
    OrganizationStatus.SUSPENDED: frozenset(
        {OrganizationStatus.ACTIVE, OrganizationStatus.ARCHIVED}
    ),
    OrganizationStatus.ARCHIVED: frozenset(),
}

# Statuses that permit normal tenant request/domain traffic.
OPERATIONAL_STATUSES: frozenset[OrganizationStatus] = frozenset({OrganizationStatus.ACTIVE})


def can_transition(current: OrganizationStatus, target: OrganizationStatus) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


def assert_transition(current: OrganizationStatus, target: OrganizationStatus) -> None:
    if current == target:
        raise OrganizationInvalidStateError(
            f"Organization is already {current.value}.",
            extensions={"from_status": current.value, "to_status": target.value},
        )
    if not can_transition(current, target):
        raise OrganizationInvalidStateError(
            f"Cannot transition Organization from {current.value} to {target.value}.",
            extensions={"from_status": current.value, "to_status": target.value},
        )


def is_operational(status: OrganizationStatus) -> bool:
    return status in OPERATIONAL_STATUSES
