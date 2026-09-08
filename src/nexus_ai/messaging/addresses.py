"""Channel address normalization (NXS-P09).

Reuses the ONE product normalization vocabulary (NXS-P06 ``normalize_identity_value``)
so a WhatsApp / SMS phone and an email address resolve to exactly the same canonical
identity the Customer plane already indexes. Ambiguity fails closed — never guessed.
"""

from __future__ import annotations

from nexus_ai.core.errors import IdentityNormalizationError
from nexus_ai.domain.customers.entities import IdentityType
from nexus_ai.domain.customers.normalization import normalize_identity_value
from nexus_ai.messaging.entities import AddressKind, MessageAddress, MessageChannel
from nexus_ai.messaging.errors import MessagingRecipientInvalidError

_CHANNEL_KIND: dict[MessageChannel, AddressKind] = {
    MessageChannel.WHATSAPP: AddressKind.PHONE,
    MessageChannel.SMS: AddressKind.PHONE,
    MessageChannel.EMAIL: AddressKind.EMAIL,
}

_KIND_IDENTITY: dict[AddressKind, IdentityType] = {
    AddressKind.PHONE: IdentityType.PHONE,
    AddressKind.EMAIL: IdentityType.EMAIL,
}


def address_kind_for(channel: MessageChannel) -> AddressKind:
    return _CHANNEL_KIND[channel]


def identity_type_for(channel: MessageChannel) -> IdentityType:
    return _KIND_IDENTITY[_CHANNEL_KIND[channel]]


def normalize_address(
    channel: MessageChannel,
    raw: str,
    *,
    default_country: str | None = None,
    display: str | None = None,
) -> MessageAddress:
    """Normalize one address for a channel. Raises :class:`MessagingRecipientInvalidError`
    on anything malformed or ambiguous."""
    kind = _CHANNEL_KIND[channel]
    try:
        normalized = normalize_identity_value(
            _KIND_IDENTITY[kind], raw, default_country=default_country
        )
    except IdentityNormalizationError as exc:
        raise MessagingRecipientInvalidError(
            "the address is not a valid " + kind.value.lower() + " address"
        ) from exc
    clean_display = None
    if display is not None:
        stripped = display.strip()
        if stripped and not any(ord(character) < 0x20 for character in stripped):
            clean_display = stripped[:200]
    return MessageAddress(kind=kind, value=normalized, display=clean_display)
