"""Import every ORM model so ``Base.metadata`` is complete.

Alembic autogeneration and the tenant schema guard both rely on this module having
imported all domain models. Add one import per new model module.
"""

from __future__ import annotations

from nexus_ai.domain.organizations.models import OrganizationRecord

__all__ = ["OrganizationRecord"]
