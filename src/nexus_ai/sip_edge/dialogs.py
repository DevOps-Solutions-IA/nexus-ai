"""Durable observation of pinned dialogs; never a new route authorization."""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError

from nexus_ai.domain.sip_edge.models import SipDialogBindingRecord, SipRouteAuthorizationRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sip_edge.contracts import Component, StrictContract, fingerprint
from nexus_ai.sip_edge.errors import SipConflictError, SipRouteDeniedError, SipRouteUnavailableError


class DialogResult(StrictContract):
    state: Literal["ESTABLISHED", "ENDED", "FAILED", "AMBIGUOUS"]
    call_id: Component
    from_tag: Component
    to_tag: Component | None = None


async def record_dialog_result(
    database: Database,
    organization_id: UUID,
    route_id: UUID,
    edge_id: UUID,
    boot_id: UUID,
    transaction_digest: str,
    result: DialogResult,
) -> str:
    result = DialogResult.model_validate(result.model_dump())
    if result.state in {"ESTABLISHED", "ENDED"} and result.to_tag is None:
        raise SipRouteDeniedError()
    digest = fingerprint({"call": result.call_id, "from": result.from_tag, "to": result.to_tag})
    try:
        async with database.tenant_transaction(organization_id) as tenant:
            route = (
                await tenant.session.execute(
                    select(SipRouteAuthorizationRecord)
                    .where(SipRouteAuthorizationRecord.id == route_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if route is None or (route.edge_id, route.boot_id, route.transaction_digest) != (
                edge_id,
                boot_id,
                transaction_digest,
            ):
                raise SipRouteDeniedError()
            binding = await tenant.session.get(SipDialogBindingRecord, route_id)
            if result.state in {"ESTABLISHED", "ENDED"}:
                if binding is not None and binding.dialog_digest != digest:
                    raise SipConflictError()
                if binding is None:
                    if result.state != "ESTABLISHED" or route.state != "ISSUED":
                        raise SipConflictError()
                    tenant.session.add(
                        SipDialogBindingRecord(
                            route_id=route_id, organization_id=organization_id, dialog_digest=digest
                        )
                    )
                    await tenant.session.flush()
            if route.state == result.state:
                return route.state
            allowed = {
                "ISSUED": {"ESTABLISHED", "FAILED", "AMBIGUOUS"},
                "ESTABLISHED": {"ENDED", "AMBIGUOUS"},
            }
            if result.state not in allowed.get(route.state, set()):
                raise SipConflictError()
            route.state = result.state
            await tenant.session.flush()
            return route.state
    except IntegrityError:
        raise SipConflictError() from None
    except DBAPIError:
        raise SipRouteUnavailableError() from None
