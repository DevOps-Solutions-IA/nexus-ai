"""Safe service metadata (NXS-API-001). Never exposes secrets or infrastructure detail."""

from __future__ import annotations

import platform

from pydantic import BaseModel, ConfigDict

from nexus_ai import __version__
from nexus_ai.core.config import Settings


class ServiceMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    product: str
    service: str
    version: str
    environment: str
    api_version: str
    build_sha: str
    runtime: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ServiceMetadata:
        return cls(
            product=settings.product,
            service=settings.service_name,
            version=__version__,
            environment=str(settings.environment),
            api_version=settings.api_version,
            build_sha=settings.build.commit,
            runtime=f"python-{platform.python_version()}",
        )
