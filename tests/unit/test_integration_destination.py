"""SSRF destination policy (NXS-INT-001, ADR-0056)."""

from __future__ import annotations

import pytest

from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import DestinationRule
from nexus_ai.integrations.errors import IntegrationDestinationBlockedError

BLOCKED_URLS = [
    ("file:///etc/passwd", "scheme_not_allowed"),
    ("ftp://example.com/x", "scheme_not_allowed"),
    ("gopher://example.com/x", "scheme_not_allowed"),
    ("ssh://example.com/x", "scheme_not_allowed"),
    ("https://user:pass@example.com/", "url_credentials"),
    ("https://example.com/#frag", "url_fragment"),
    ("https://127.0.0.1/x", "blocked_address"),
    ("https://127.0.0.5/x", "blocked_address"),
    ("http://169.254.169.254/latest/meta-data/", "blocked_address"),
    ("https://[::1]/x", "blocked_address"),
    ("https://[fd00:ec2::254]/x", "blocked_address"),
    ("https://10.1.2.3/x", "blocked_address"),
    ("https://172.16.9.9/x", "blocked_address"),
    ("https://192.168.1.1/x", "blocked_address"),
    ("https://100.64.0.1/x", "blocked_address"),
    ("https://0.0.0.0/x", "blocked_address"),
    ("https://[::ffff:127.0.0.1]/x", "blocked_address"),
    ("https://[fe80::1]/x", "blocked_address"),
    ("https://224.0.0.1/x", "blocked_address"),
    ("https://192.0.2.10/x", "blocked_address"),
    ("https://localhost/x", "blocked_host"),
    ("https://foo.localhost/x", "blocked_host"),
    ("https://svc.internal/x", "blocked_host"),
    ("https://db.local/x", "blocked_host"),
    ("https:///nohost", "no_host"),
    ("https://exa mple.com/x", "invalid_host"),
]


@pytest.mark.parametrize(("url", "reason"), BLOCKED_URLS)
def test_config_time_blocks(url: str, reason: str) -> None:
    policy = DestinationPolicy()
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        policy.validate_url(url)
    assert excinfo.value.extensions["reason"] == reason


def test_public_https_is_allowed() -> None:
    policy = DestinationPolicy()
    validated = policy.validate_url("https://api.example.com:8443/v1/things")
    assert validated.host == "api.example.com"
    assert validated.port == 8443
    assert validated.is_https


def test_public_http_allowed_by_default_but_not_when_required() -> None:
    assert DestinationPolicy().validate_url("http://api.example.com/x").port == 80
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        DestinationPolicy(require_https=True).validate_url("http://api.example.com/x")
    assert excinfo.value.extensions["reason"] == "https_required"
    with pytest.raises(IntegrationDestinationBlockedError):
        DestinationPolicy().validate_url(
            "http://api.example.com/x", rule=DestinationRule(require_https=True)
        )


def test_rule_blocked_host_suffix() -> None:
    policy = DestinationPolicy()
    rule = DestinationRule(blocked_hosts=("example.com",))
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        policy.validate_url("https://api.example.com/x", rule=rule)
    assert excinfo.value.extensions["reason"] == "rule_blocked_host"


def test_resolve_rejects_dns_to_private() -> None:
    policy = DestinationPolicy(resolver=lambda host, port: ["10.0.0.9"])
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        policy.resolve("https://rebind.example.com/x")
    assert excinfo.value.extensions["reason"] == "dns_to_blocked"


def test_resolve_rejects_mixed_answer_with_one_private() -> None:
    policy = DestinationPolicy(resolver=lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(IntegrationDestinationBlockedError):
        policy.resolve("https://api.example.com/x")


def test_resolve_pins_public_addresses() -> None:
    policy = DestinationPolicy(resolver=lambda host, port: ["93.184.216.34", "93.184.216.35"])
    resolved = policy.resolve("https://api.example.com/x")
    assert resolved.addresses == ("93.184.216.34", "93.184.216.35")
    assert resolved.host == "api.example.com"


def test_resolve_dns_failure_is_blocked() -> None:
    def _boom(host: str, port: int) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        DestinationPolicy(resolver=_boom).resolve("https://api.example.com/x")
    assert excinfo.value.extensions["reason"] == "dns_failure"


def test_resolve_empty_answer_is_blocked() -> None:
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        DestinationPolicy(resolver=lambda host, port: []).resolve("https://api.example.com/x")
    assert excinfo.value.extensions["reason"] == "dns_empty"


def test_rule_blocked_cidr_at_resolve() -> None:
    policy = DestinationPolicy(resolver=lambda host, port: ["93.184.216.34"])
    rule = DestinationRule(blocked_cidrs=("93.184.216.0/24",))
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        policy.resolve("https://api.example.com/x", rule=rule)
    assert excinfo.value.extensions["reason"] == "rule_blocked_cidr"


def test_rule_invalid_cidr_is_reported() -> None:
    policy = DestinationPolicy(resolver=lambda host, port: ["93.184.216.34"])
    rule = DestinationRule(blocked_cidrs=("not-a-cidr",))
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        policy.resolve("https://api.example.com/x", rule=rule)
    assert excinfo.value.extensions["reason"] == "rule_invalid_cidr"


def test_literal_ip_public_resolves_to_itself() -> None:
    resolved = DestinationPolicy().resolve("https://93.184.216.34/x")
    assert resolved.addresses == ("93.184.216.34",)


def test_allow_loopback_flag_is_test_only_escape_hatch() -> None:
    policy = DestinationPolicy(allow_loopback=True, resolver=lambda host, port: ["127.0.0.1"])
    resolved = policy.resolve("http://mock.integration.test:9000/x")
    assert resolved.addresses == ("127.0.0.1",)
    # scheme / credential checks still apply
    with pytest.raises(IntegrationDestinationBlockedError):
        policy.validate_url("file:///etc/passwd")


def test_invalid_port() -> None:
    with pytest.raises(IntegrationDestinationBlockedError) as excinfo:
        DestinationPolicy().validate_url("https://api.example.com:0/x")
    assert excinfo.value.extensions["reason"] == "invalid_port"
