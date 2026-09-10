"""Deterministic, bounded, tenant-scoped agent context assembly (NXS-P13, ADR-0092).

The :class:`AgentContextBuilder` turns Nexus-owned facts into a bounded message context.
Rules: bounded size, deterministic ordering, tenant-scoped reads only, no cross-tenant
references, no secrets, no raw credentials, no arbitrary DB dump, no unbounded history,
no hidden provider payloads. The truncation / windowing policy is explicit and lives
here.
"""

from __future__ import annotations

from dataclasses import dataclass

from nexus_ai.agents.entities import AgentSession
from nexus_ai.agents.errors import AgentContextInvalidError
from nexus_ai.agents.models.base import ModelMessage, ModelRole
from nexus_ai.core.config import AgentRuntimeSettings
from nexus_ai.domain.customers.repository import ConversationRepository, CustomerRepository
from nexus_ai.infrastructure.tenant_session import TenantSession


@dataclass(frozen=True, slots=True)
class AssembledContext:
    context_block: str
    history: tuple[ModelMessage, ...]
    user_input: str


@dataclass(frozen=True, slots=True)
class PriorTurn:
    sequence: int
    input_text: str
    response_text: str | None


class AgentContextBuilder:
    def __init__(self, settings: AgentRuntimeSettings) -> None:
        self._cfg = settings

    async def build(
        self,
        *,
        tenant: TenantSession,
        session: AgentSession,
        prior_turns: list[PriorTurn],
        user_input: str,
        channel: str,
    ) -> AssembledContext:
        """Assemble a bounded context. ``tenant`` is the RLS-scoped session for
        ``session.organization_id`` — every read below is therefore tenant-scoped and a
        cross-tenant id resolves to nothing (fail closed)."""
        facts: list[str] = [f"channel: {channel}"]

        if session.customer_id is not None:
            customer = await CustomerRepository(tenant).by_id(session.customer_id)
            if customer is None:
                raise AgentContextInvalidError(
                    "the referenced customer is not in this Organization"
                )
            facts.append(f"customer: {_clip(customer.display_name, 120)}")

        if session.conversation_id is not None:
            conversation = await ConversationRepository(tenant).by_id(session.conversation_id)
            if conversation is None:
                raise AgentContextInvalidError(
                    "the referenced conversation is not in this Organization"
                )
            facts.append(f"conversation-status: {conversation.status.value}")
            if conversation.subject:
                facts.append(f"conversation-subject: {_clip(conversation.subject, 200)}")

        context_block = "\n".join(facts)

        window = prior_turns[-self._cfg.max_context_messages :]
        history: list[ModelMessage] = []
        budget = self._cfg.max_context_bytes
        per_message = self._cfg.max_context_message_chars
        for turn in window:
            for role, text in (
                (ModelRole.USER, turn.input_text),
                (ModelRole.ASSISTANT, turn.response_text),
            ):
                if not text:
                    continue
                clipped = _clip(text, per_message)
                budget -= len(clipped.encode("utf-8"))
                if budget <= 0:
                    break
                history.append(ModelMessage(role=role, content=clipped))
            if budget <= 0:
                break

        return AssembledContext(
            context_block=context_block[: self._cfg.max_context_bytes],
            history=tuple(history),
            user_input=_clip(user_input, self._cfg.max_input_chars),
        )


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
