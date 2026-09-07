"""Dashboard schema, registry and generator behavior (NXS-DASH-001)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.core.errors import UnknownWidgetError, UnsupportedDashboardVersionError
from nexus_ai.domain.dashboard.generator import GENERATOR
from nexus_ai.domain.dashboard.registry import (
    WIDGET_REGISTRY,
    validate_generated_schema,
    widget_spec,
)
from nexus_ai.domain.dashboard.schema import DashboardSchema, DashboardWidget

NOW = dt.datetime(2026, 9, 7, 19, 0, 0, tzinfo=dt.UTC)


class TestRegistryAllowList:
    def test_registered_widgets_resolve(self) -> None:
        for spec in WIDGET_REGISTRY:
            assert widget_spec(spec.key.value) is spec

    @pytest.mark.parametrize("key", ["conversations.list", "whatsapp.traffic", "admin.shell", "x"])
    def test_unregistered_widgets_fail_closed(self, key: str) -> None:
        with pytest.raises(UnknownWidgetError):
            widget_spec(key)

    def test_registry_is_small_and_correct(self) -> None:
        # The baseline registry only covers control-plane data that genuinely exists by
        # P05 — nothing fakes future-domain widgets.
        assert {spec.key.value for spec in WIDGET_REGISTRY} == {
            "organization.profile",
            "organization.provisioning_status",
            "organization.settings_summary",
        }


class TestGeneratorDeterminism:
    def test_generation_is_deterministic_modulo_timestamp(self) -> None:
        org = uuid.uuid7()
        first = GENERATOR.generate(organization_id=org, revision=1, generated_at=NOW)
        second = GENERATOR.generate(organization_id=org, revision=1, generated_at=NOW)
        assert first.model_dump(mode="json") == second.model_dump(mode="json")

    def test_order_is_stable(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        orders = [w.order for section in schema.sections for w in section.widgets]
        assert orders == sorted(orders)
        assert schema.schema_version == 1
        assert schema.revision == 1

    def test_generated_schema_revalidates(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        validate_generated_schema(schema)


class TestPermissionFiltering:
    def test_owner_sees_everything(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        owner_permissions = {
            "organization:read",
            "organization:provision:read",
            "organization:settings:read",
            "dashboard:read",
        }
        filtered = schema.permission_filtered(owner_permissions)
        total = sum(len(s.widgets) for s in schema.sections)
        filtered_total = sum(len(s.widgets) for s in filtered.sections)
        assert filtered_total == total

    def test_member_sees_only_permitted_widgets(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        minimal = schema.permission_filtered({"organization:read"})
        for section in minimal.sections:
            for widget in section.widgets:
                assert "organization:read" in widget.required_permissions

    def test_viewer_with_nothing_sees_empty_sections(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        filtered = schema.permission_filtered(set())
        assert all(len(section.widgets) == 0 for section in filtered.sections)


class TestSchemaRejection:
    def test_unsupported_version_fails_closed(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        tampered = schema.model_copy(update={"schema_version": 99})
        with pytest.raises(UnsupportedDashboardVersionError):
            validate_generated_schema(tampered)

    def test_unknown_widget_in_stored_schema_fails_closed(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        evil = DashboardWidget(
            widget_key="tool_engine.execute",
            title="Execute",
            data_source="tool_engine.execute",
            order=1,
            visible=True,
            required_permissions=(),
        )
        tampered = schema.model_copy(
            update={"sections": (schema.sections[0].model_copy(update={"widgets": (evil,)}),)}
        )
        with pytest.raises(UnknownWidgetError):
            validate_generated_schema(tampered)

    def test_widget_with_arbitrary_code_config_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DashboardWidget(
                widget_key="organization.profile",
                title="Profile",
                data_source="organization.profile",
                order=1,
                required_permissions=(),
                config={"exec": "os.system('rm -rf /')"},
            )
        with pytest.raises(ValidationError):
            DashboardWidget(
                widget_key="organization.profile",
                title="Profile",
                data_source="organization.profile",
                order=1,
                required_permissions=(),
                config={"sql": "DROP TABLE organizations"},
            )

    def test_extra_fields_rejected(self) -> None:
        schema = GENERATOR.generate(organization_id=uuid.uuid7(), revision=1, generated_at=NOW)
        raw = schema.model_dump()
        raw["sections"][0]["widgets"][0]["script_url"] = "https://evil.example/x.js"
        with pytest.raises(ValidationError):
            DashboardSchema.model_validate(raw)
