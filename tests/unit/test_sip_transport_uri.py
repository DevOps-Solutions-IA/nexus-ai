"""Typed SIP destination construction has no header or URI injection seam."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def edge_script(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setitem(sys.modules, "KSR", SimpleNamespace())
    spec = importlib.util.spec_from_file_location(
        "nxs_test_edge", Path("infrastructure/kamailio/edge.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("transport", ["UDP", "TCP", "TLS"])
@pytest.mark.parametrize("host", ["10.2.3.4", "fd01::1"])
def test_exact_transport_and_bracketed_ipv6(edge_script: Any, transport: str, host: str) -> None:
    literal = f"[{host}]" if ":" in host else host
    scheme = "sips" if transport == "TLS" else "sip"
    assert edge_script.sip_destination(host, 5061, transport, "+12025550123") == (
        f"{scheme}:+12025550123@{literal}:5061;transport={transport.lower()}"
    )


@pytest.mark.parametrize(
    "host",
    [
        "sip:10.2.3.4",
        "10.2.3.4;transport=udp",
        "user@10.2.3.4",
        "10.2.3.4\r\nRoute:x",
        "10.2.3.4?header=x",
    ],
)
def test_uri_components_cannot_be_injected(edge_script: Any, host: str) -> None:
    with pytest.raises(ValueError):
        edge_script.sip_destination(host, 5061, "TLS")


@pytest.mark.parametrize(
    "transport,port,user",
    [
        ("WS", 5060, None),
        ("TLS", True, None),
        ("TCP", 65536, None),
        ("UDP", 5060, "+123;transport=tls"),
    ],
)
def test_transport_port_and_user_are_bounded(
    edge_script: Any,
    transport: str,
    port: Any,
    user: str | None,
) -> None:
    with pytest.raises(ValueError):
        edge_script.sip_destination("10.2.3.4", port, transport, user)
