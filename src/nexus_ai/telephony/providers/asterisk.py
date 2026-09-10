"""Asterisk provider adapter — the ARI control boundary (NXS-P11, ADR-0084).

Nexus AI's telephony engine is **Asterisk 22 LTS**. P11 integrates it through the
**Asterisk REST Interface (ARI)** only:

* **ARI** drives application-level call control (originate, hangup, DTMF, bridges,
  Stasis events). It is the sole boundary this adapter uses.
* **AMI** is intentionally NOT used from the application. AMI is a broad operational
  control channel; the only justified AMI use is out-of-band operations tooling, which
  is out of P11 scope.
* **PJSIP** configures SIP endpoints and trunks *inside* Asterisk. The application never
  writes PJSIP config or dialplan — a trunk / endpoint is provisioned by operations and
  referenced from ``telephony_accounts.configuration`` by a bounded alias only.
* **Media bridges** — ARI ``bridges`` group call legs. NXS-P12 attaches an external
  voice stream to a bridge via ``externalMedia``; P11 only records the bridge / stream
  identifiers on a ``MediaSession`` (see ADR-0086).

This adapter issues **only** ARI REST calls, through the injected governed transport
(SSRF-safe, TLS-verified, bounded-timeout). It never runs a shell, never emits a
dialplan string, and never returns the ARI credential. Inbound Stasis events reach Nexus
as signed webhooks from an operations-owned bridge (asterisk -> nxs), verified by
``verify_signed_webhook``.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote

from nexus_ai.telephony.entities import TelephonyAccount
from nexus_ai.telephony.errors import (
    TelephonyConfigInvalidError,
    TelephonyProviderError,
    TelephonyProviderTimeoutError,
)
from nexus_ai.telephony.providers.base import (
    OutboundCallResult,
    OutboundCallSpec,
    TelephonyTransport,
    TransportError,
    WebhookContext,
    WebhookParseResult,
    provider_call_failure,
    verify_signed_webhook,
)
from nexus_ai.telephony.providers.fake import _parse_event, _parse_inbound  # normalized parsers


class AsteriskAdapter:
    key = "asterisk"

    def _ari_base(self, account: TelephonyAccount) -> str:
        base = account.configuration.get("ari_base")
        if not isinstance(base, str) or not base.startswith("https://"):
            raise TelephonyConfigInvalidError(
                "the Asterisk account requires a validated https 'ari_base'"
            )
        return base.rstrip("/")

    @staticmethod
    def _auth_header(secret: Any) -> dict[str, str]:
        from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError

        if secret is None:
            raise TelephonyProviderError("the Asterisk account has no ARI credential")
        try:
            user = str(secret.field("ari_user"))
            password = str(secret.field("ari_password"))
        except IntegrationCredentialUnavailableError as exc:
            raise TelephonyProviderError("the Asterisk ARI credential is incomplete") from exc
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    async def _call(
        self,
        transport: TelephonyTransport,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> Any:
        try:
            response = await transport.request(
                method=method, url=url, headers=headers, body=body, timeout_seconds=None
            )
        except TransportError as exc:
            if exc.timeout:
                raise TelephonyProviderTimeoutError("Asterisk ARI timed out") from exc
            raise TelephonyProviderError(
                "the Asterisk ARI connection failed", retryable=not exc.connect
            ) from exc
        if response.status_code >= 400:
            raise provider_call_failure(
                "Asterisk ARI rejected the request", status_code=response.status_code
            )
        if not response.body:
            return {}
        try:
            return json.loads(response.body)
        except json.JSONDecodeError as exc:
            raise TelephonyProviderError("Asterisk ARI returned a malformed body") from exc

    async def create_outbound_call(
        self, spec: OutboundCallSpec, secret: Any, transport: TelephonyTransport
    ) -> OutboundCallResult:
        base = self._ari_base(spec.account)
        stasis_app = str(spec.account.configuration.get("stasis_app") or "nexus")
        prefix = str(spec.account.configuration.get("sip_endpoint_prefix") or "PJSIP")
        if spec.destination_kind == "PHONE":
            endpoint = f"{prefix}/{spec.destination_value.lstrip('+')}"
        else:
            endpoint = f"{prefix}/{spec.destination_value}"
        params = {
            "endpoint": endpoint,
            "app": stasis_app,
            "callerId": spec.caller_id_e164,
        }
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
        headers = {**self._auth_header(secret), "Content-Type": "application/json"}
        payload = await self._call(
            transport,
            method="POST",
            url=f"{base}/channels?{query}",
            headers=headers,
            body=b"{}",
        )
        channel_id = str(payload.get("id") or "")
        if not channel_id:
            raise TelephonyProviderError("Asterisk ARI did not return a channel id")
        return OutboundCallResult(provider_call_id=channel_id, accepted=True)

    async def hangup_call(
        self,
        account: TelephonyAccount,
        provider_call_id: str,
        secret: Any,
        transport: TelephonyTransport,
    ) -> None:
        base = self._ari_base(account)
        try:
            await self._call(
                transport,
                method="DELETE",
                url=f"{base}/channels/{quote(provider_call_id, safe='')}",
                headers=self._auth_header(secret),
                body=None,
            )
        except TelephonyProviderError as exc:
            if exc.extensions.get("provider_status") == 404:
                return
            raise

    async def send_dtmf(
        self,
        account: TelephonyAccount,
        provider_call_id: str,
        digits: str,
        secret: Any,
        transport: TelephonyTransport,
    ) -> None:
        base = self._ari_base(account)
        channel = quote(provider_call_id, safe="")
        await self._call(
            transport,
            method="POST",
            url=f"{base}/channels/{channel}/dtmf?dtmf={quote(digits, safe='')}",
            headers=self._auth_header(secret),
            body=b"",
        )

    def verify_webhook(
        self,
        account: TelephonyAccount,
        ctx: WebhookContext,
        secret: Any,
        *,
        tolerance_seconds: int,
    ) -> None:
        from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError
        from nexus_ai.telephony.errors import TelephonyWebhookInvalidError

        if secret is None:
            raise TelephonyWebhookInvalidError("the account has no webhook secret configured")
        try:
            webhook_secret = str(secret.field("webhook_secret"))
        except IntegrationCredentialUnavailableError as exc:
            raise TelephonyWebhookInvalidError(
                "the account has no webhook secret configured"
            ) from exc
        headers = {k.lower(): v for k, v in ctx.headers.items()}
        verify_signed_webhook(
            body=ctx.body,
            provided_signature=headers.get("x-telephony-signature"),
            provided_timestamp=headers.get("x-telephony-timestamp"),
            secret=webhook_secret,
            tolerance_seconds=tolerance_seconds,
        )

    def webhook_challenge(self, account: TelephonyAccount, ctx: WebhookContext) -> str | None:
        return ctx.query.get("challenge")

    def parse_webhook(self, account: TelephonyAccount, ctx: WebhookContext) -> WebhookParseResult:
        try:
            payload = json.loads(ctx.body or b"{}")
        except json.JSONDecodeError as exc:
            raise TelephonyProviderError("the Stasis callback body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise TelephonyProviderError("the Stasis callback body must be a JSON object")
        # The operations bridge forwards a normalized envelope: {"kind": ..., ...}. The
        # raw Asterisk Stasis event shape is normalized there, not here, so this adapter
        # never depends on Asterisk's internal event vocabulary.
        kind = str(payload.get("kind") or "").lower()
        if kind == "inbound":
            return WebhookParseResult(inbound_call=_parse_inbound(payload))
        return WebhookParseResult(events=(_parse_event(payload),))
