"""Versioned API surface assembly (NXS-API-001)."""

from __future__ import annotations

from fastapi import APIRouter

from nexus_ai.api.system import system_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(system_router)
