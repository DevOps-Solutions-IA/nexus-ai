"""The provider-neutral model adapter contract (NXS-P13, ADR-0091).

A :class:`ModelProviderAdapter` is a *normalizer*: it turns a Nexus-owned
:class:`ModelRequest` into a provider-specific REST call and the provider's response into
a Nexus-owned :class:`ModelResponse`, and nothing else. It never owns tenancy, tool
authority, credentials-at-rest, business logic or agent reasoning. Every provider REST
call goes through an injected :class:`ModelHttpTransport` — in production the NXS-P07
governed HTTP executor, so a model adapter can never reach an arbitrary or internal URL.
No vendor-specific field ever leaves this package.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ModelRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ModelFinishReason(StrEnum):
    STOP = "STOP"
    TOOL_CALLS = "TOOL_CALLS"
    LENGTH = "LENGTH"
    CONTENT_FILTER = "CONTENT_FILTER"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    """A model's request to call a tool. ``id`` and ``name`` are bounded; ``arguments``
    is an already-parsed JSON object the runtime validates against the agent's allow-list
    and the tool's input schema before ever reaching the Tool Engine."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: ModelRole
    content: str
    #: set only on a ``tool`` message — the id of the tool call this result answers
    tool_call_id: str | None = None
    #: set only on an ``assistant`` message that requested tools
    tool_calls: tuple[ModelToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelToolSpec:
    """A tool offered to the model — derived from a registered NXS-P08 tool that is on the
    agent's allow-list. The model may *request* it; authority is still enforced by the
    runtime and the Tool Engine."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """Everything an adapter needs for one model call — already assembled and bounded by
    the runtime. No credential, no raw provider config, no arbitrary URL.

    ``provider_api_base`` is the TRUSTED provider REST origin. It comes from the model
    provider account's validated ``api_base`` (SSRF-checked at registration and again by
    the governed transport), NEVER from a caller or the model.
    """

    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[ModelToolSpec, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None
    stop: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    provider_api_base: str = ""
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """The provider-neutral result of one model call (ADR-0091 §structured contract)."""

    assistant_content: str
    tool_calls: tuple[ModelToolCall, ...]
    finish_reason: ModelFinishReason
    usage: ModelUsage
    provider_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelStreamChunk:
    delta_text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: ModelFinishReason | None = None
    usage: ModelUsage | None = None


# --- transport ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes


class HttpError(Exception):
    def __init__(self, message: str, *, timeout: bool = False, connect: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout
        self.connect = connect


class ModelHttpTransport(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse: ...


@dataclass(slots=True)
class ModelError(Exception):
    """An adapter-internal failure. The runtime maps it to the stable agent taxonomy."""

    detail: str
    status_code: int | None = None
    provider_code: str | None = None
    timeout: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.detail)


class ModelProviderAdapter(Protocol):
    """The stable model provider contract the runtime consumes. A ``fake`` adapter is a
    first-class provider; a production adapter (OpenAI-compatible, Anthropic, …) speaks
    the same interface. Nothing outside ``nexus_ai.agents.models`` imports a vendor shape.
    """

    key: str

    async def generate(
        self, request: ModelRequest, secret: Any, http: ModelHttpTransport
    ) -> ModelResponse: ...

    def stream(
        self, request: ModelRequest, secret: Any, http: ModelHttpTransport
    ) -> AsyncIterator[ModelStreamChunk]: ...
