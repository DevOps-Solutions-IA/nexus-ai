"""Platform model transport: fixed destination, bounded P07 HTTP, no tenant vault."""

from dataclasses import replace
from urllib.parse import urlsplit

from pydantic import SecretStr

from nexus_ai.agents.models.base import HttpResponse, ModelRequest, ModelResponse
from nexus_ai.agents.models.openai_compatible import OpenAiCompatibleModelProvider
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
from nexus_ai.integrations.executor import GovernedHttpExecutor, OutboundRequest
from nexus_ai.sentinel.errors import SentinelDenied


class SentinelModelTransport:
    def __init__(self, executor: GovernedHttpExecutor, *, api_origin: str) -> None:
        parsed = urlsplit(api_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise SentinelDenied("invalid_platform_model_origin")
        settings = executor._settings
        if (
            settings.max_redirects != 0
            or settings.max_response_bytes > 32768
            or settings.max_request_bytes > 32768
        ):
            raise SentinelDenied("unbounded_platform_model_transport")
        self.origin = api_origin.rstrip("/")
        self._executor = executor

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse:
        if (
            method != "POST"
            or url != self.origin + "/v1/chat/completions"
            or body is None
            or len(body) > 32768
            or timeout_seconds is None
            or not 0 < timeout_seconds <= 120
            or not set(headers) <= {"Authorization", "Content-Type", "Accept"}
        ):
            raise SentinelDenied("model_transport_request_denied")
        response = await self._executor.send(
            OutboundRequest(
                method=method,
                url=url,
                headers=headers,
                body=body,
                content_type="application/json",
                timeout_seconds=timeout_seconds,
            )
        )
        if response.final_url != url or len(response.body) > 32768:
            raise SentinelDenied("model_transport_response_denied")
        return HttpResponse(response.status_code, {}, response.body)


class SentinelProviderClient:
    def __init__(self, transport: SentinelModelTransport) -> None:
        self._transport = transport
        self._provider = OpenAiCompatibleModelProvider()

    async def generate(self, request: ModelRequest, credential: SecretStr) -> ModelResponse:
        if request.tools:
            raise SentinelDenied("sentinel_tools_forbidden")
        material = SecretMaterial(
            CredentialType.API_KEY, {"api_key": credential.get_secret_value()}
        )
        return await self._provider.generate(
            replace(request, provider_api_base=self._transport.origin), material, self._transport
        )
