"""Deterministic dashboard schema generator (NXS-DASH-001).

Configuration + registry → ``DashboardSchema``. Deterministic for identical inputs:
stable section/widget ordering (registry order), registry-controlled permissions,
registry-controlled data sources. A tenant can neither inject widgets nor elevate
its own permissions through configuration — the generator ignores anything outside
the allow-list and the permission filter projects the schema for the viewer.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from nexus_ai.domain.dashboard.registry import WIDGET_REGISTRY, registry_schema_version
from nexus_ai.domain.dashboard.schema import (
    DASHBOARD_SCHEMA_VERSION,
    DashboardSchema,
    DashboardSection,
    DashboardWidget,
)


class DashboardGenerator:
    """Generates and re-validates the baseline tenant dashboard schema."""

    def generate(
        self,
        *,
        organization_id: UUID,
        revision: int,
        generated_at: dt.datetime | None = None,
    ) -> DashboardSchema:
        sections: list[DashboardSection] = []
        by_section: dict[str, list[DashboardWidget]] = {}
        for order, spec in enumerate(WIDGET_REGISTRY):
            by_section.setdefault(spec.section, []).append(
                DashboardWidget(
                    widget_key=spec.key.value,
                    title=spec.title,
                    data_source=spec.data_source.value,
                    order=order,
                    visible=True,
                    required_permissions=spec.required_permissions,
                    config=dict(spec.default_config),
                )
            )
        for section_order, (section_key, widgets) in enumerate(by_section.items()):
            sections.append(
                DashboardSection(
                    section_key=section_key,
                    title=section_key.title(),
                    order=section_order,
                    widgets=tuple(widgets),
                )
            )
        return DashboardSchema(
            schema_version=DASHBOARD_SCHEMA_VERSION,
            organization_id=organization_id,
            revision=revision,
            generated_at=generated_at or dt.datetime.now(dt.UTC),
            sections=tuple(sections),
        )

    def current_version(self) -> int:
        return registry_schema_version()


GENERATOR = DashboardGenerator()
