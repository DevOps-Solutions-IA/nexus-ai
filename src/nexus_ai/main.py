"""ASGI entrypoint. ``uvicorn nexus_ai.main:app`` serves the production application."""

from __future__ import annotations

from nexus_ai.application import create_app

app = create_app()
