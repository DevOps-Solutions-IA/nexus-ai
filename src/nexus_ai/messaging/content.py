"""Safe message-content handling (NXS-P09).

Every inbound and outbound body passes through here. Guarantees:

* size bounds per channel (text bytes, HTML bytes, SMS characters);
* HTML is treated as opaque text — the platform NEVER executes or renders it, and a
  ``<script>`` payload is data, not code;
* email header fields (subject, Message-ID, In-Reply-To, References, display names) are
  CRLF / header-injection safe — a newline or a bare CR anywhere in a header value is
  rejected;
* control characters (except tab / newline in a body) and unpaired surrogates are
  rejected as malformed Unicode.
"""

from __future__ import annotations

import unicodedata

from nexus_ai.messaging.entities import (
    MAX_HTML_BYTES,
    MAX_SMS_TEXT_CHARS,
    MAX_TEXT_BYTES,
    MessageChannel,
    MessageContent,
    MessageContentType,
)
from nexus_ai.messaging.errors import MessagingPayloadInvalidError

_HEADER_FORBIDDEN = ("\r", "\n", "\x00")
_BODY_CONTROL = {chr(index) for index in range(0x20)} - {"\t", "\n", "\r"} | {"\x7f"}


def _has_unpaired_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def scrub_header_token(value: str, *, max_length: int = 255) -> str:
    """Strip CR / LF / NUL / other control characters and unpaired surrogates from an
    UNTRUSTED provider token (e.g. an inbound email Message-ID) and bound its length.
    Never raises — used where a provider is malformed rather than a caller hostile."""
    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        character
        for character in normalized
        if ord(character) > 0x20 and character != "\x7f" and not 0xD800 <= ord(character) <= 0xDFFF
    )
    return cleaned[:max_length]


def sanitize_header_value(value: str, *, field: str, max_length: int = 255) -> str:
    """Return a CRLF-safe, bounded header value or raise. Used for subject and every
    email threading key, and for any display name that reaches a provider."""
    normalized = unicodedata.normalize("NFC", value)
    if any(token in normalized for token in _HEADER_FORBIDDEN):
        raise MessagingPayloadInvalidError(
            f"the {field} value must not contain CR, LF or NUL (header injection)"
        )
    if _has_unpaired_surrogate(normalized):
        raise MessagingPayloadInvalidError(f"the {field} value contains malformed Unicode")
    if len(normalized) > max_length:
        raise MessagingPayloadInvalidError(f"the {field} value exceeds {max_length} characters")
    return normalized


def _check_body_text(text: str, *, field: str, max_bytes: int) -> str:
    normalized = unicodedata.normalize("NFC", text)
    if _has_unpaired_surrogate(normalized):
        raise MessagingPayloadInvalidError(f"the {field} contains malformed Unicode")
    if any(character in _BODY_CONTROL for character in normalized):
        raise MessagingPayloadInvalidError(f"the {field} contains disallowed control characters")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise MessagingPayloadInvalidError(f"the {field} exceeds {max_bytes} bytes")
    return normalized


def normalize_content(channel: MessageChannel, content: MessageContent) -> MessageContent:
    """Validate + normalize a content payload for a channel. Raises
    :class:`MessagingPayloadInvalidError` on any violation."""
    text = content.text
    html = content.html
    if text is not None:
        text = _check_body_text(text, field="message text", max_bytes=MAX_TEXT_BYTES)
    if html is not None:
        if channel is not MessageChannel.EMAIL:
            raise MessagingPayloadInvalidError(f"{channel.value} messages may not carry HTML")
        html = _check_body_text(html, field="message html", max_bytes=MAX_HTML_BYTES)
    if channel is MessageChannel.SMS:
        if content.content_type is not MessageContentType.TEXT:
            raise MessagingPayloadInvalidError("SMS supports plain text only")
        if text is not None and len(text) > MAX_SMS_TEXT_CHARS:
            raise MessagingPayloadInvalidError(
                f"an SMS message may not exceed {MAX_SMS_TEXT_CHARS} characters"
            )
    if channel is MessageChannel.WHATSAPP and content.content_type is MessageContentType.HTML:
        raise MessagingPayloadInvalidError("WhatsApp does not support HTML content")
    return content.model_copy(update={"text": text, "html": html})
