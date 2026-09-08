"""SSRF destination policy — the Integration Hub's most security-critical primitive
(NXS-INT-001, ADR-0056).

Every outbound URL — an integration base URL, an OAuth token URL, an OpenAPI server URL,
a webhook target — passes this policy at BOTH configuration time and execution time. The
policy is deny-by-default for anything that is not a public ``http``/``https`` endpoint:

* scheme allow-list — only ``http`` and ``https`` (``file``, ``ftp``, ``gopher``,
  ``data``, ``ssh``, ``ws`` … are all rejected);
* no credentials in the URL, no fragment;
* literal-IP hosts are classified directly; hostnames are classified by name AND, at
  execution time, every resolved address is re-classified (DNS-to-private and DNS
  rebinding are both caught because the executor connects to a pinned, already-validated
  address);
* blocked address space: loopback, link-local (incl. the ``169.254.169.254`` cloud
  metadata address and ``fd00:ec2::254``), private (RFC 1918 / unique-local), carrier NAT
  (``100.64.0.0/10``), multicast, reserved, unspecified, documentation ranges,
  IPv4-mapped / NAT64 forms of any of the above, and any address Python does not consider
  globally routable;
* hardened environments (staging / production) additionally require ``https``.

The policy only ever gets STRICTER per integration (:class:`DestinationRule`) — a
per-integration rule can add blocked hosts/CIDRs, never open one.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from nexus_ai.integrations.entities import DestinationRule
from nexus_ai.integrations.errors import IntegrationDestinationBlockedError

_ALLOWED_SCHEMES = ("http", "https")

#: Additional CIDR ranges blocked beyond what ``ipaddress`` flags via
#: ``is_private`` / ``is_global`` / ``is_reserved`` / ``is_multicast``.
_EXTRA_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # RFC 6598 carrier-grade NAT
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "240.0.0.0/4",  # reserved / future use
        "255.255.255.255/32",
        "64:ff9b::/96",  # NAT64
        "100::/64",  # discard-only
        "2001:db8::/32",  # documentation
    )
)

#: Hostnames (exact or dotted suffix) that never leave the box.
_BLOCKED_NAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
)
_BLOCKED_NAMES = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)

Resolver = Callable[[str, int], Sequence[str]]


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True, slots=True)
class ValidatedDestination:
    scheme: str
    host: str
    port: int
    is_literal_ip: bool

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"


@dataclass(frozen=True, slots=True)
class ResolvedDestination:
    scheme: str
    host: str
    port: int
    #: Every resolved address, each already classified as safe. The executor connects to
    #: one of these directly (address pinning) so nothing re-resolves between check and
    #: connect.
    addresses: tuple[str, ...]

    @property
    def is_https(self) -> bool:
        return self.scheme == "https"


def _blocked_ip_reason(raw_ip: str) -> str | None:
    try:
        address = ipaddress.ip_address(raw_ip)
    except ValueError:
        return "unparseable address"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        mapped = _blocked_ip_reason(str(address.ipv4_mapped))
        return None if mapped is None else f"IPv4-mapped {mapped}"
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or getattr(address, "is_private", False)
    ):
        return "non-public address space"
    if not address.is_global:
        return "not globally routable"
    for network in _EXTRA_BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return f"blocked range {network}"
    return None


def _is_ip_literal(host: str) -> str | None:
    """Return the bare address if ``host`` is an IP literal, else ``None``."""
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def _name_is_blocked(host: str) -> str | None:
    lowered = host.lower().rstrip(".")
    if lowered in _BLOCKED_NAMES:
        return "loopback hostname"
    for suffix in _BLOCKED_NAME_SUFFIXES:
        if lowered.endswith(suffix):
            return f"blocked hostname suffix {suffix!r}"
    return None


class DestinationPolicy:
    """Stateless SSRF gate. One instance per process, shared across integrations."""

    def __init__(
        self,
        *,
        require_https: bool = False,
        resolver: Resolver | None = None,
        allow_loopback: bool = False,
    ) -> None:
        self._require_https = require_https
        self._resolver = resolver or _default_resolver
        #: TEST ONLY. Permits LOOPBACK destinations (127.0.0.0/8, ::1) so a test can point
        #: an integration at a local mock server. Everything else — link-local and the
        #: cloud-metadata address, RFC 1918, unique-local, multicast, reserved — stays
        #: blocked. Production NEVER constructs the policy with this flag; the security
        #: matrix asserts the default policy blocks loopback too.
        self._allow_loopback = allow_loopback

    def _ip_reason(self, raw_ip: str) -> str | None:
        reason = _blocked_ip_reason(raw_ip)
        if reason is None or not self._allow_loopback:
            return reason
        try:
            address = ipaddress.ip_address(raw_ip.strip("[]"))
        except ValueError:  # pragma: no cover - resolver returns valid addresses
            return reason
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return None if address.is_loopback else reason

    # -- configuration-time --------------------------------------------------------

    def validate_url(
        self, raw: str, *, rule: DestinationRule | None = None
    ) -> ValidatedDestination:
        """Structural + literal-IP + name checks. No DNS. Raises on any violation."""
        rule = rule or DestinationRule()
        parts = urlsplit(raw.strip())
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            raise IntegrationDestinationBlockedError(
                f"scheme {parts.scheme!r} is not allowed", reason="scheme_not_allowed"
            )
        if parts.username or parts.password:
            raise IntegrationDestinationBlockedError(
                "credentials in the URL are not allowed", reason="url_credentials"
            )
        if parts.fragment:
            raise IntegrationDestinationBlockedError(
                "a URL fragment is not allowed", reason="url_fragment"
            )
        host = parts.hostname
        if not host:
            raise IntegrationDestinationBlockedError("the URL has no host", reason="no_host")
        scheme = parts.scheme.lower()
        try:
            port = parts.port if parts.port is not None else (443 if scheme == "https" else 80)
        except ValueError:
            raise IntegrationDestinationBlockedError(
                "invalid port", reason="invalid_port"
            ) from None
        if not (1 <= port <= 65535):
            raise IntegrationDestinationBlockedError("invalid port", reason="invalid_port")

        # Address/host blocking is the primary SSRF signal — check it before the
        # https requirement so a blocked target reports "blocked_address", not
        # "https_required".
        literal = _is_ip_literal(host)
        is_literal_ip = literal is not None
        if literal is not None:
            ip_reason = self._ip_reason(literal)
            if ip_reason is not None:
                raise IntegrationDestinationBlockedError(
                    f"destination address is blocked: {ip_reason}", reason="blocked_address"
                )
        if not is_literal_ip:
            name_reason = _name_is_blocked(host)
            if name_reason is not None:
                raise IntegrationDestinationBlockedError(
                    f"destination host is blocked: {name_reason}", reason="blocked_host"
                )
            if not _HOSTNAME_RE.match(host):
                raise IntegrationDestinationBlockedError(
                    "the destination host is not a valid hostname", reason="invalid_host"
                )
            self._apply_rule_hosts(host, rule)

        require_https = self._require_https or rule.require_https
        if require_https and scheme != "https":
            raise IntegrationDestinationBlockedError(
                "this environment requires https", reason="https_required"
            )
        return ValidatedDestination(
            scheme=scheme, host=host, port=port, is_literal_ip=is_literal_ip
        )

    # -- execution-time -----------------------------------------------------------

    def resolve(self, raw: str, *, rule: DestinationRule | None = None) -> ResolvedDestination:
        """Re-validate, then resolve the host and classify EVERY address. The returned
        addresses are safe to connect to directly (pins against DNS rebinding)."""
        rule = rule or DestinationRule()
        validated = self.validate_url(raw, rule=rule)
        if validated.is_literal_ip:
            literal = validated.host.strip("[]")
            self._apply_rule_cidrs(literal, rule)
            return ResolvedDestination(
                scheme=validated.scheme,
                host=validated.host,
                port=validated.port,
                addresses=(literal,),
            )
        try:
            resolved = list(dict.fromkeys(self._resolver(validated.host, validated.port)))
        except OSError as exc:
            raise IntegrationDestinationBlockedError(
                "the destination host could not be resolved", reason="dns_failure"
            ) from exc
        if not resolved:
            raise IntegrationDestinationBlockedError(
                "the destination host did not resolve", reason="dns_empty"
            )
        for address in resolved:
            reason = self._ip_reason(address)
            if reason is not None:
                raise IntegrationDestinationBlockedError(
                    f"destination resolves to a blocked address: {reason}",
                    reason="dns_to_blocked",
                )
            self._apply_rule_cidrs(address, rule)
        return ResolvedDestination(
            scheme=validated.scheme,
            host=validated.host,
            port=validated.port,
            addresses=tuple(resolved),
        )

    # -- per-integration rule --------------------------------------------------

    @staticmethod
    def _apply_rule_hosts(host: str, rule: DestinationRule) -> None:
        lowered = host.lower().rstrip(".")
        for blocked in rule.blocked_hosts:
            b = blocked.lower().rstrip(".")
            if lowered == b or lowered.endswith("." + b):
                raise IntegrationDestinationBlockedError(
                    "destination host is blocked by the integration policy",
                    reason="rule_blocked_host",
                )

    @staticmethod
    def _apply_rule_cidrs(address: str, rule: DestinationRule) -> None:
        try:
            addr = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - resolver returns normalized addresses
            return
        for cidr in rule.blocked_cidrs:
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                raise IntegrationDestinationBlockedError(
                    f"the integration policy has an invalid CIDR {cidr!r}",
                    reason="rule_invalid_cidr",
                ) from None
            if addr.version == network.version and addr in network:
                raise IntegrationDestinationBlockedError(
                    "destination address is blocked by the integration policy",
                    reason="rule_blocked_cidr",
                )
