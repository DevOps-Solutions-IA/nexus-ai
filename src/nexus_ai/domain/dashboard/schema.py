"""Versioned, data-driven dashboard schema (NXS-DASH-001).

The schema is a validated backend contract a future frontend renders: sections of
widgets with stable keys, titles, visibility, ordering, a data-source identifier and
the permissions the VIEWER must hold. No executable code, no SQL, no URLs and no
backend function references can ever appear in a widget definition.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

DASHBOARD_SCHEMA_VERSION: int = 1

_CONFIG_KEY = re.compile(r"\A[a-z][a-z0-9_]{0,47}\Z")
#: Conservative denylist: widget config keys that could ever be interpreted as code,
#: SQL, URLs or backend callbacks are structurally forbidden — config is data, only.
_CONFIG_FORBIDDEN_PREFIXES = (
    "exec",
    "eval",
    "sql",
    "command",
    "script",
    "url",
    "hook",
    "callback",
    "import",
    "code",
)

_Key = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*){0,3}$"
    ),
]
_Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class DashboardWidget(BaseModel):
    """One validated widget instance in a dashboard section.

    ``widget_key`` and ``data_source`` must exist in the allow-listed registry;
    ``required_permissions`` are registry-controlled (the generator fills them —
    a stored configuration can never add or remove them). ``config`` accepts only
    bounded, flat, primitive values — no executable code, no SQL, no URLs, no
    backend function references can ever appear in a widget definition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    widget_key: _Key
    title: _Title
    data_source: _Key
    order: int = Field(ge=0, le=10_000)
    visible: bool = True
    required_permissions: tuple[str, ...] = ()
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def _config_is_bounded_primitive_data(cls, value: dict[str, object]) -> dict[str, object]:
        if len(value) > 32:
            raise ValueError("widget config has too many entries")
        for key, item in value.items():
            if not _CONFIG_KEY.match(key):
                raise ValueError(f"unsafe widget config key: {key!r}")
            if key.startswith(_CONFIG_FORBIDDEN_PREFIXES):
                raise ValueError(f"forbidden widget config key: {key!r}")
            if not _is_primitive_config_value(item):
                raise ValueError(f"unsafe widget config value for {key!r}")
        return value


def _is_primitive_config_value(value: object) -> bool:
    """Only flat primitive data: str/int/float/bool/None or a bounded list of those."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return (isinstance(value, str) and len(value) <= 256) or not isinstance(value, str)
    if isinstance(value, list):
        return len(value) <= 16 and all(_is_primitive_config_value(item) for item in value)
    return False


class DashboardSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    section_key: _Key
    title: _Title
    order: int = Field(ge=0, le=10_000)
    widgets: tuple[DashboardWidget, ...] = ()


class DashboardSchema(BaseModel):
    """The canonical generated schema. Independently validateable by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    organization_id: UUID
    revision: int
    generated_at: dt.datetime
    sections: tuple[DashboardSection, ...]

    def permission_filtered(self, viewer_permissions: set[str]) -> DashboardSchema:
        """Project the schema to exactly what this viewer may see (audit requirement:
        an ordinary member gets only their permitted view)."""

        def _allowed(widget: DashboardWidget) -> bool:
            return set(widget.required_permissions) <= viewer_permissions

        sections = tuple(
            DashboardSection(
                section_key=section.section_key,
                title=section.title,
                order=section.order,
                widgets=tuple(w for w in section.widgets if _allowed(w)),
            )
            for section in self.sections
        )
        return self.model_copy(update={"sections": sections})
