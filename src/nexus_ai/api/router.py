"""Versioned API surface assembly (NXS-API-001)."""

from __future__ import annotations

from fastapi import APIRouter

from nexus_ai.api.agents import agents_router
from nexus_ai.api.auth import auth_router
from nexus_ai.api.conversations import conversations_router
from nexus_ai.api.customers import customers_router
from nexus_ai.api.integrations import integrations_router
from nexus_ai.api.messaging import messaging_router, messaging_webhooks_router
from nexus_ai.api.organizations import organizations_router
from nexus_ai.api.otp import otp_router
from nexus_ai.api.system import system_router
from nexus_ai.api.telephony import telephony_router, telephony_webhooks_router
from nexus_ai.api.tools import tools_router
from nexus_ai.api.voice import voice_router, voice_webhooks_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(system_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(organizations_router)
api_v1_router.include_router(customers_router)
api_v1_router.include_router(conversations_router)
api_v1_router.include_router(integrations_router)
api_v1_router.include_router(tools_router)
api_v1_router.include_router(messaging_router)
api_v1_router.include_router(messaging_webhooks_router)
api_v1_router.include_router(otp_router)
api_v1_router.include_router(telephony_router)
api_v1_router.include_router(telephony_webhooks_router)
api_v1_router.include_router(voice_router)
api_v1_router.include_router(voice_webhooks_router)
api_v1_router.include_router(agents_router)
