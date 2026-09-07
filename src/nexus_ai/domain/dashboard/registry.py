"""Allow-listed widget and data-source registry (NXS-DASH-001).

Only registry members may appear in a dashboard schema. Each widget declares its
canonical key, the schema version it belongs to, the data source it may bind, the
permissions a VIEWER must hold, and its allowed configuration model. A tenant can
never inject an unregistered widget, reference an unregistered data source, or grant
itself a permission through dashboard configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from nexus_ai.core.errors import UnknownWidgetError, UnsupportedDashboardVersionError
from nexus_ai.domain.dashboard.schema import DASHBOARD_SCHEMA_VERSION, DashboardSchema


class DataSourceKey(StrEnum):
    ORGANIZATION_PROFILE = "organization.profile"
    PROVISIONING_STATUS = "organization.provisioning_status"
    ORGANIZATION_SETTINGS = "organization.settings"


class WidgetKey(StrEnum):
    ORGANIZATION_PROFILE = "organization.profile"
    PROVISIONING_STATUS = "organization.provisioning_status"
    SETTINGS_SUMMARY = "organization.settings_summary"


@dataclass(frozen=True, slots=True)
class WidgetSpec:
    """A registered widget definition (allow-list entry)."""

    key: WidgetKey
    schema_version: int
    title: str
    section: str
    data_source: DataSourceKey
    required_permissions: tuple[str, ...]
    default_config: dict[str, object] = field(default_factory=dict)


#: The small, correct baseline registry: control-plane widgets that genuinely exist by
#: P05. Future domains register their own widgets in their own phases — nothing here
#: fakes conversations, channels, campaigns or any later-domain data.
WIDGET_REGISTRY: tuple[WidgetSpec, ...] = (
    WidgetSpec(
        key=WidgetKey.ORGANIZATION_PROFILE,
        schema_version=DASHBOARD_SCHEMA_VERSION,
        title="Organization Profile",
        section="organization",
        data_source=DataSourceKey.ORGANIZATION_PROFILE,
        required_permissions=("organization:read",),
    ),
    WidgetSpec(
        key=WidgetKey.PROVISIONING_STATUS,
        schema_version=DASHBOARD_SCHEMA_VERSION,
        title="Provisioning Status",
        section="organization",
        data_source=DataSourceKey.PROVISIONING_STATUS,
        required_permissions=("organization:provision:read",),
    ),
    WidgetSpec(
        key=WidgetKey.SETTINGS_SUMMARY,
        schema_version=DASHBOARD_SCHEMA_VERSION,
        title="Organization Settings",
        section="organization",
        data_source=DataSourceKey.ORGANIZATION_SETTINGS,
        required_permissions=("organization:settings:read",),
    ),
)

_WIDGETS_BY_KEY = {spec.key: spec for spec in WIDGET_REGISTRY}


def registry_schema_version() -> int:
    return DASHBOARD_SCHEMA_VERSION


def widget_spec(key: str) -> WidgetSpec:
    """Resolve a widget key through the allow-list. Fails closed for anything else."""
    try:
        resolved = WidgetKey(key)
    except ValueError:
        raise UnknownWidgetError(
            "the widget is not registered on this platform", extensions={"widget_key": key}
        ) from None
    spec = _WIDGETS_BY_KEY[resolved]
    if spec.schema_version != DASHBOARD_SCHEMA_VERSION:
        raise UnsupportedDashboardVersionError(
            "the widget belongs to an unsupported dashboard schema version",
            extensions={
                "widget_key": key,
                "widget_schema_version": spec.schema_version,
                "supported_version": DASHBOARD_SCHEMA_VERSION,
            },
        )
    return spec


def validate_generated_schema(schema: DashboardSchema) -> None:
    """Every stored/loaded schema MUST revalidate against the registry before it is
    served: unknown widgets and unsupported versions fail closed (audit requirement)."""
    if schema.schema_version != DASHBOARD_SCHEMA_VERSION:
        raise UnsupportedDashboardVersionError(
            "the stored dashboard schema version is not supported",
            extensions={
                "stored_version": schema.schema_version,
                "supported_version": DASHBOARD_SCHEMA_VERSION,
            },
        )
    for section in schema.sections:
        for widget in section.widgets:
            spec = widget_spec(widget.widget_key)
            if widget.data_source != spec.data_source.value:
                raise UnknownWidgetError(
                    "the widget references an unregistered data source",
                    extensions={"widget_key": widget.widget_key},
                )
            if set(widget.required_permissions) != set(spec.required_permissions):
                raise UnknownWidgetError(
                    "the widget's permission requirements do not match the registry",
                    extensions={"widget_key": widget.widget_key},
                )
