"""Telephony adversarial tests (NXS-P11: NXS-TEL-001)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from nexus_ai.telephony.entities import (
    CreateAccountRequest,
    CreateCallRequest,
    RegisterPhoneNumberRequest,
    SendDtmfRequest,
    TelephonyProvider,
)
from nexus_ai.telephony.errors import (
    TelephonyCallNotFoundError,
    TelephonyConfigInvalidError,
    TelephonyInvalidDestinationError,
    TelephonyNotAuthorizedError,
    TelephonyWebhookInvalidError,
)
from nexus_ai.telephony.providers.base import WebhookContext

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SECRET = {"webhook_secret": "tel-secret", "ari_user": "u", "ari_password": "p"}


async def _account(stack: Any, org_id: Any) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            provider=TelephonyProvider.FAKE,
            slug=f"tel-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"default_country": "1"},
        ),
    )
    await stack.service.store_account_credential(org_id, account.id, _SECRET)
    return await stack.service.get_account(org_id, account.id)


async def _number(stack: Any, org_id: Any, account: Any, e164: str = "+14155550100") -> Any:
    n = await stack.service.register_number(
        org_id, RegisterPhoneNumberRequest(account_id=account.id, e164=e164)
    )
    return await stack.service.set_number_verified(org_id, n.id, verified=True)


def _signed(secret: str, body: bytes, *, ts: int | None = None) -> WebhookContext:
    stamp = str(ts or int(time.time()))
    digest = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": stamp},
        query={},
        body=body,
    )


async def test_cross_tenant_call_and_number_access_fail_closed(
    telephony_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    account = await _account(telephony_stack, org_a.id)
    number = await _number(telephony_stack, org_a.id, account)
    call = await telephony_stack.service.create_call(
        org_a.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id, from_number_id=number.id, destination="+14155550199"
        ),
    )
    with pytest.raises(TelephonyCallNotFoundError):
        await telephony_stack.service.get_call(org_b.id, call.id)
    from nexus_ai.telephony.errors import TelephonyAccountNotFoundError

    with pytest.raises(TelephonyAccountNotFoundError):
        await telephony_stack.service.get_account(org_b.id, account.id)


async def test_forged_cross_tenant_call_row_refused_by_fk(
    telephony_stack: Any, make_organization: Any
) -> None:
    org_a = await make_organization()
    org_b = await make_organization()
    account_b = await _account(telephony_stack, org_b.id)
    async with telephony_stack.database.tenant_transaction(org_a.id) as tenant:
        with pytest.raises(IntegrityError):
            await tenant.session.execute(
                text(
                    "INSERT INTO telephony_calls (id, organization_id, account_id, direction, "
                    "state, state_rank, provider, from_address, to_address, legs) VALUES "
                    "(:id, :org, :acct, 'OUTBOUND', 'CREATED', 0, 'fake', 'x', 'y', '[]')"
                ),
                {"id": uuid4(), "org": org_a.id, "acct": account_b.id},
            )


async def test_caller_id_must_be_an_owned_verified_number(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    other_org = await make_organization()
    other_account = await _account(telephony_stack, other_org.id)
    other_number = await _number(telephony_stack, other_org.id, other_account, "+14155550111")
    # a number id from another Organization
    with pytest.raises(Exception) as exc:
        await telephony_stack.service.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=other_number.id,
                destination="+14155550199",
            ),
        )
    assert exc.value.code in ("NXS_TELEPHONY_NUMBER_NOT_FOUND", "NXS_TELEPHONY_CONFIG_INVALID")


@pytest.mark.parametrize(
    "destination",
    [
        "sip:evil@10.0.0.1",
        "+1415555\r\nRoute: <sip:evil>",
        "1;branch=x",
        "user@host",
        "+" + "1" * 40,
    ],
)
async def test_destination_injection_is_rejected(
    telephony_stack: Any, make_organization: Any, destination: str
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    # oversized / injection payloads that pass the request model are caught by canonicalize
    try:
        request = CreateCallRequest(
            provider_account_id=account.id,
            from_number_id=number.id,
            destination=destination,
        )
    except ValidationError:
        return  # rejected at the schema — also acceptable
    with pytest.raises(TelephonyInvalidDestinationError):
        await telephony_stack.service.create_call(org.id, None, request)


async def test_unsigned_wrong_secret_and_replayed_webhooks_are_refused(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    body = json.dumps({"event_id": uuid4().hex, "call_id": "x", "event": "RINGING"}).encode()

    with pytest.raises(TelephonyWebhookInvalidError):
        await telephony_stack.inbound.receive(
            "fake", account.webhook_token, WebhookContext("POST", {}, {}, body)
        )
    with pytest.raises(TelephonyWebhookInvalidError):
        await telephony_stack.inbound.receive(
            "fake", account.webhook_token, _signed("wrong-secret", body)
        )
    from nexus_ai.telephony.errors import TelephonyWebhookReplayError

    with pytest.raises(TelephonyWebhookReplayError):
        await telephony_stack.inbound.receive(
            "fake",
            account.webhook_token,
            _signed("tel-secret", body, ts=int(time.time()) - 5000),
        )


async def test_inbound_tenancy_never_comes_from_the_payload(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    await _number(telephony_stack, org.id, account, "+14155550100")
    # the payload dials a number this account does NOT own -> fail closed
    body = json.dumps(
        {
            "kind": "inbound",
            "event_id": uuid4().hex,
            "call_id": f"pc-{uuid4().hex}",
            "from": "+14155550142",
            "to": "+19998887777",
        }
    ).encode()
    with pytest.raises(TelephonyNotAuthorizedError):
        await telephony_stack.inbound.receive(
            "fake", account.webhook_token, _signed("tel-secret", body)
        )


async def test_credentials_never_appear_in_events_or_call_rows(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    call = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id, from_number_id=number.id, destination="+14155550199"
        ),
    )
    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        blob = json.dumps(
            [
                dict(r._mapping)
                for r in (
                    await tenant.session.execute(
                        text(
                            "SELECT envelope FROM event_outbox WHERE event_type LIKE 'telephony.%'"
                        )
                    )
                ).all()
            ],
            default=str,
        )
        row = (
            (
                await tenant.session.execute(
                    text("SELECT * FROM telephony_calls WHERE id = :i"), {"i": call.id}
                )
            )
            .one()
            ._mapping
        )
    for secret in ("tel-secret", "ari_password", "ari_user", "Authorization"):
        assert secret not in blob
        assert secret not in json.dumps(dict(row), default=str)


async def test_dtmf_oversized_and_arbitrary_are_rejected(
    telephony_stack: Any, make_organization: Any
) -> None:
    with pytest.raises(ValidationError):
        SendDtmfRequest(digits="12;DROP TABLE")
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    call = await telephony_stack.service.create_call(
        org.id,
        None,
        CreateCallRequest(
            provider_account_id=account.id, from_number_id=number.id, destination="+14155550199"
        ),
    )
    from nexus_ai.telephony.errors import TelephonyDtmfInvalidError, TelephonyInvalidStateError

    with pytest.raises((TelephonyDtmfInvalidError, TelephonyInvalidStateError)):
        await telephony_stack.service.send_dtmf(org.id, call.id, SendDtmfRequest(digits="1" * 100))


async def test_disabled_subsystem_refuses_calls(
    telephony_stack: Any, make_organization: Any
) -> None:
    from nexus_ai.telephony.service import TelephonyService

    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    number = await _number(telephony_stack, org.id, account)
    disabled_settings = telephony_stack.settings.model_copy(
        update={
            "telephony": telephony_stack.settings.telephony.model_copy(update={"enabled": False})
        }
    )
    disabled = TelephonyService(
        disabled_settings,
        telephony_stack.database,
        telephony_stack.event_platform.publisher,
        telephony_stack.vault,
        telephony_stack.transport,
    )
    with pytest.raises(TelephonyConfigInvalidError):
        await disabled.create_call(
            org.id,
            None,
            CreateCallRequest(
                provider_account_id=account.id,
                from_number_id=number.id,
                destination="+14155550199",
            ),
        )
