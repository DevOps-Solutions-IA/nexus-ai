"""The Asterisk <-> voice-provider media-bridge boundary (NXS-P12, ADR-0088).

NXS-P11 ADR-0086 says P12 attaches an external voice stream to an ACTIVE media session's
Asterisk bridge. P12 NEVER exposes an arbitrary ARI URL, SIP URI, dialplan, AMI action,
hostname, ``externalMedia`` destination or UDP target. A :class:`MediaBridgePlan` is
generated ENTIRELY from validated inputs:

* ``bridge_id`` comes from the trusted NXS-P11 media-session row (re-validated here);
* the media-gateway host / port come from the voice provider account's bounded,
  allow-listed configuration — never from a caller, a provider payload or a URL;
* the audio format is the negotiated :class:`AudioFormat`.

The plan is a description an operations-owned media gateway executes; P12 itself opens no
Asterisk connection. The gateway address is refused if it is loopback, link-local, a
cloud metadata address, multicast, unspecified or otherwise not a private unicast target
(SSRF / internal-network-access defense).
"""

from __future__ import annotations

import ipaddress
import re
import secrets
from dataclasses import dataclass

from nexus_ai.voice.audio import AudioFormat
from nexus_ai.voice.errors import VoiceConfigInvalidError

_BRIDGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?:\.(?!-)[A-Za-z0-9-]{1,63})*$")
_STREAM_REF = re.compile(r"^vs-[a-f0-9]{24}$")

_MIN_PORT = 1_024
_MAX_PORT = 65_535

#: Config keys the media bridge reads from a voice provider account.
GATEWAY_HOST_KEY = "media_gateway_host"
GATEWAY_PORT_KEY = "media_gateway_port"


@dataclass(frozen=True, slots=True)
class MediaBridgePlan:
    """A validated instruction for the ops media gateway. No credential, no ARI URL."""

    bridge_id: str
    stream_ref: str
    gateway_host: str
    gateway_port: int
    audio_format: AudioFormat


def new_stream_ref() -> str:
    """A fresh internal stream identifier P12 owns (never provider-supplied)."""
    return f"vs-{secrets.token_hex(12)}"


def _reject_unsafe_ip(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname, not a literal IP — validated by pattern + resolved by the gateway
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or (address.version == 4 and str(address).startswith("169.254."))
        or str(address) in {"169.254.169.254", "100.100.100.200"}  # cloud metadata
    ):
        raise VoiceConfigInvalidError("the media-gateway address is not a permitted target")
    if not address.is_private:
        raise VoiceConfigInvalidError("the media-gateway address must be a private unicast target")


def resolve_gateway(configuration: dict[str, object]) -> tuple[str, int]:
    """Extract and validate the media-gateway host / port from account configuration."""
    raw_host = configuration.get(GATEWAY_HOST_KEY)
    raw_port = configuration.get(GATEWAY_PORT_KEY)
    if not isinstance(raw_host, str) or not raw_host.strip():
        raise VoiceConfigInvalidError(
            f"the voice provider account must configure {GATEWAY_HOST_KEY!r} to bridge media"
        )
    host = raw_host.strip()
    if len(host) > 253 or (not _HOSTNAME.match(host) and _is_not_ip(host)):
        raise VoiceConfigInvalidError("the media-gateway host is not a valid hostname or IP")
    _reject_unsafe_ip(host)
    if isinstance(raw_port, bool) or not isinstance(raw_port, int | str):
        raise VoiceConfigInvalidError("the media-gateway port is not an integer")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise VoiceConfigInvalidError("the media-gateway port is not an integer") from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        raise VoiceConfigInvalidError(
            f"the media-gateway port must be between {_MIN_PORT} and {_MAX_PORT}"
        )
    return host, port


def _is_not_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def plan_media_bridge(
    *,
    bridge_id: str | None,
    configuration: dict[str, object],
    audio_format: AudioFormat,
    stream_ref: str | None = None,
) -> MediaBridgePlan:
    """Build a validated :class:`MediaBridgePlan`, or fail closed."""
    if not bridge_id or not _BRIDGE_ID.match(bridge_id):
        raise VoiceConfigInvalidError(
            "the media session has no usable bridge identifier for an external voice stream"
        )
    ref = stream_ref or new_stream_ref()
    if not _STREAM_REF.match(ref):
        raise VoiceConfigInvalidError("the media stream reference is malformed")
    host, port = resolve_gateway(configuration)
    return MediaBridgePlan(
        bridge_id=bridge_id,
        stream_ref=ref,
        gateway_host=host,
        gateway_port=port,
        audio_format=audio_format,
    )
