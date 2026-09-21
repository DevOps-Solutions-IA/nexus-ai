"""Fenced UDP/TCP/TLS signaling; PostgreSQL grants every new relay."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

import KSR


def sip_destination(host, port, transport, user=None):
    address = ipaddress.ip_address(host)
    if transport not in {"UDP", "TCP", "TLS"} or type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid destination transport")
    if user is not None and not re.fullmatch(r"\+[1-9][0-9]{6,14}", user):
        raise ValueError("invalid destination user")
    literal = f"[{address}]" if address.version == 6 else str(address)
    scheme = "sips" if transport == "TLS" else "sip"
    return f"{scheme}:{user + '@' if user else ''}{literal}:{port};transport={transport.lower()}"


def certificate_digest(encoded):
    if not isinstance(encoded, str) or len(encoded) > 16384:
        raise ValueError("invalid observed certificate")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(encoded)).hexdigest()


class Edge:
    def __init__(self):
        configuration = json.loads(Path("/run/secrets/nxs-sip-edge.json").read_text())
        self.edge_id = str(uuid.UUID(configuration["edge_id"]))
        if not re.fullmatch(r"[a-f0-9]{64}", os.environ.get("NXS_SIP_TOPOLOGY_KEY", "")):
            raise ValueError("explicit topology masking key required")
        self.boot_id = str(uuid.uuid4())
        self.secret = bytes.fromhex(configuration["hmac_secret"])
        self.resolver_host = str(ipaddress.ip_address(configuration["resolver_host"]))
        self.resolver_port = configuration["resolver_port"]
        self.peers = configuration["peers"]
        self.tls_targets = {}
        for target in configuration.get("tls_targets", []):
            if type(target["port"]) is not int or not 1024 <= target["port"] <= 65535:
                raise ValueError("invalid TLS target port")
            key = (str(ipaddress.ip_address(target["host"])), int(target["port"]))
            digest = target["certificate_sha256"]
            if key in self.tls_targets or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid TLS target identity")
            self.tls_targets[key] = digest
        if len(self.tls_targets) > 256:
            raise ValueError("unbounded TLS targets")
        identity = Path("/run/secrets/sip/identity.key")
        if not stat.S_ISREG(identity.stat().st_mode) or identity.stat().st_mode & 0o007:
            raise ValueError("private TLS identity must not be accessible to others")
        for filename in ("identity.key", "identity.pem", "ca.pem"):
            if Path("/run/secrets/sip", filename).stat().st_mode & 0o022:
                raise ValueError("TLS material must not be writable by group or others")
        identity_context = ssl.create_default_context(cafile="/run/secrets/sip/ca.pem")
        identity_context.load_cert_chain("/run/secrets/sip/identity.pem", str(identity))
        identity_details = ssl._ssl._test_decode_cert("/run/secrets/sip/identity.pem")
        if not (
            ssl.cert_time_to_seconds(identity_details["notBefore"])
            <= time.time()
            < ssl.cert_time_to_seconds(identity_details["notAfter"])
        ):
            raise ValueError("TLS identity outside validity interval")
        self.edge_hosts = tuple(configuration.get("edge_hosts", ()))
        if any(peer.get("direction") == "OUTBOUND" for peer in self.peers) and (
            not 1 <= len(self.edge_hosts) <= 16
            or any(str(ipaddress.ip_address(host)) != host for host in self.edge_hosts)
        ):
            raise ValueError("explicit outbound ingress addresses required")
        self.limits = configuration["limits"]
        for key, maximum in (
            ("messages_per_second", 1000),
            ("pending_resolvers", 2),
            ("dialogs", 10000),
        ):
            value = self.limits[key]
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("invalid resource bound")
        if len(self.secret) < 32 or not 1024 <= self.resolver_port <= 65535:
            raise ValueError("invalid resolver configuration")
        if any(peer.get("transport", "UDP") != "TLS" for peer in self.peers) and (
            configuration.get("isolated_test_network") is not True
        ):
            raise ValueError("plain UDP/TCP transports require isolated network")
        if not 1 <= len(self.peers) <= 256:
            raise ValueError("bounded trusted peers required")
        for peer in self.peers:
            uuid.UUID(peer["id"])
            network = ipaddress.ip_network(peer["network"], strict=True)
            if network.prefixlen == 0:
                raise ValueError("wildcard peer forbidden")
            transport = peer.get("transport", "UDP")
            if transport not in {"UDP", "TCP", "TLS"}:
                raise ValueError("unsupported peer transport")
            if transport == "TLS" and not re.fullmatch(
                r"[a-f0-9]{64}", peer.get("certificate_sha256", "")
            ):
                raise ValueError("TLS peer identity required")
        self.resolver_tls = ssl.create_default_context(cafile="/run/secrets/resolver-ca.pem")
        self.resolver_tls.minimum_version = ssl.TLSVersion.TLSv1_2

    def child_init(self, rank):
        KSR.xlog.xwarn("nxs_edge_ready\n")
        return 0

    def observed_transport(self):
        transport = str(KSR.pv.get("$proto")).upper()
        if transport not in {"UDP", "TCP", "TLS"}:
            raise ValueError("unknown received transport")
        return transport

    def observed_certificate(self):
        if KSR.tls.is_peer_verified() <= 0:
            raise ValueError("TLS peer not verified")
        return certificate_digest(KSR.pv.get("$tls_peer_raw_cert"))

    def ksr_tls_event(self, message, event):
        try:
            if event != "tls:connection-out":
                raise ValueError("unknown TLS event")
            key = (
                str(ipaddress.ip_address(KSR.pv.get("$si"))),
                int(KSR.pv.get("$sp")),
            )
            expected = self.tls_targets.get(key)
            if expected is None or not hmac.compare_digest(
                expected, certificate_digest(KSR.pv.get("$tls_peer_raw_cert"))
            ):
                raise ValueError("TLS target identity mismatch")
        except Exception:
            KSR.set_drop()
            KSR.xlog.xwarn("nxs_tls_target_denied\n")
            return 0
        return 1

    def resolver(self, path, payload):
        if not self.reserve("pending", self.limits["pending_resolvers"]):
            raise ValueError("resolver capacity exhausted")
        try:
            return self.request_resolver(path, payload)
        finally:
            self.release("pending")

    def reserve(self, key, maximum):
        KSR.htable.sht_lock("nxs_bounds", key)
        try:
            variable = "$sht(nxs_bounds=>" + key + ")"
            count = KSR.pv.get(variable) or 0
            if count >= maximum:
                return False
            KSR.pv.seti(variable, count + 1)
            return True
        finally:
            KSR.htable.sht_unlock("nxs_bounds", key)

    def release(self, key):
        KSR.htable.sht_lock("nxs_bounds", key)
        try:
            variable = "$sht(nxs_bounds=>" + key + ")"
            KSR.pv.seti(variable, max(0, (KSR.pv.get(variable) or 0) - 1))
        finally:
            KSR.htable.sht_unlock("nxs_bounds", key)

    def rate_allowed(self, peer):
        key = "rate_" + peer["id"]
        KSR.htable.sht_lock("nxs_bounds", key)
        try:
            variable = "$sht(nxs_bounds=>" + key + ")"
            state = json.loads(KSR.pv.get(variable) or "[0,0]")
            now = int(time.monotonic())
            if state[0] != now:
                state = [now, 0]
            if state[1] >= self.limits["messages_per_second"]:
                return False
            state[1] += 1
            KSR.pv.sets(variable, json.dumps(state))
            return True
        finally:
            KSR.htable.sht_unlock("nxs_bounds", key)

    def request_resolver(self, path, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > 8192:
            raise ValueError("request too large")
        timestamp, nonce = int(time.time()), secrets.token_hex(32)
        signed = "\n".join(
            (
                "nxs-sip-v1",
                self.edge_id,
                self.boot_id,
                "POST",
                path,
                hashlib.sha256(body).hexdigest(),
                str(timestamp),
                nonce,
            )
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "X-NXS-Edge": self.edge_id,
            "X-NXS-Boot": self.boot_id,
            "X-NXS-Timestamp": str(timestamp),
            "X-NXS-Nonce": nonce,
            "X-NXS-Signature": hmac.digest(self.secret, signed, "sha256").hex(),
        }
        deadline = self.route_deadline
        available = deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError("resolver deadline exceeded")
        connection = http.client.HTTPSConnection(
            self.resolver_host,
            self.resolver_port,
            timeout=min(0.8, available / 2),
            context=self.resolver_tls,
        )
        timer = None
        try:
            connection.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or connection.sock is None:
                raise TimeoutError("resolver deadline exceeded")
            connected_socket = connection.sock
            connected_socket.settimeout(remaining)

            def expire():
                with suppress(OSError):
                    connected_socket.shutdown(socket.SHUT_RDWR)

            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            connection.request("POST", path, body, headers)
            response = connection.getresponse()
            encoded = response.read(8193)
            if response.status != 200 or len(encoded) > 8192:
                KSR.xlog.xwarn("nxs_resolver_rejected status=" + str(response.status) + "\n")
                raise ValueError("resolver denied")
            return json.loads(encoded)
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            connection.close()

    def deny(self, status=403, reason="Forbidden"):
        KSR.xlog.xwarn("nxs_edge_denied status=" + str(status) + "\n")
        if KSR.pv.get("$rm") != "ACK":
            KSR.sl.send_reply(status, reason)
        return 1

    def ksr_route_reply(self, message):
        self.route_deadline = time.monotonic() + 1.8
        try:
            reply_transport = KSR.pv.get("$avp(nxs_reply_transport)") or "UDP"
            if (
                KSR.pv.get("$si") != KSR.pv.get("$avp(nxs_reply_host)")
                or self.observed_transport() != reply_transport
                or (
                    reply_transport == "UDP"
                    and str(KSR.pv.get("$sp")) != KSR.pv.get("$avp(nxs_reply_port)")
                )
                or (
                    reply_transport == "TLS"
                    and self.observed_certificate() != KSR.pv.get("$avp(nxs_reply_certificate)")
                )
            ):
                KSR.set_drop()
                return 0
            status = int(KSR.pv.get("$rs"))
            method = KSR.pv.get("$rm")
            if method == "INVITE" and 100 < status < 200 and KSR.pv.get("$hdr(RSeq)"):
                reliable = KSR.pv.get("$hdr(RSeq)")
                if not re.fullmatch(r"[1-9][0-9]{0,9}", reliable) or int(reliable) > 2147483647:
                    KSR.set_drop()
                    return 0
                if KSR.dialog.dlg_get(KSR.pv.get("$ci"), KSR.pv.get("$ft"), KSR.pv.get("$tt")) > 0:
                    current = KSR.pv.get("$dlg_var(reliable_sequence)")
                    if not current or int(reliable) >= int(current):
                        KSR.pv.sets("$dlg_var(reliable_sequence)", reliable)
            handle = KSR.pv.get("$avp(nxs_handle)")
            state = None
            if method == "INVITE" and 200 <= status < 300:
                state = "ESTABLISHED"
            elif method == "BYE" and 200 <= status < 300:
                state = "ENDED"
            elif method == "INVITE" and status >= 300 and KSR.pv.get("$avp(nxs_initial)"):
                state = "FAILED"
            if state and handle:
                self.resolver(
                    "/internal/sip/result",
                    {
                        "handle": handle,
                        "result": {
                            "state": state,
                            "call_id": KSR.pv.get("$ci"),
                            "from_tag": KSR.pv.get("$tt")
                            if KSR.pv.get("$avp(nxs_reverse)")
                            else KSR.pv.get("$ft"),
                            "to_tag": (
                                KSR.pv.get("$ft")
                                if KSR.pv.get("$avp(nxs_reverse)")
                                else KSR.pv.get("$tt")
                            )
                            or None,
                        },
                    },
                )
            if state in {"ENDED", "FAILED"}:
                self.finish_dialog()
        except Exception:
            KSR.xlog.xwarn("nxs_route_result_unavailable\n")
        return 1

    def finish_dialog(self):
        key = KSR.pv.get("$avp(nxs_protocol)")
        owner = KSR.pv.get("$avp(nxs_protocol_owner)")
        if not key or not owner:
            return
        released = False
        KSR.htable.sht_lock("nxs_dialogs", key)
        try:
            variable = "$sht(nxs_dialogs=>" + key + ")"
            state = json.loads(KSR.pv.get(variable) or "{}")
            if state.get("owner") == owner:
                KSR.pv.unset(variable)
                released = True
        finally:
            KSR.htable.sht_unlock("nxs_dialogs", key)
        if released:
            self.release("dialogs")

    def ksr_request_route(self, message):
        self.route_deadline = time.monotonic() + 1.8
        self.reserved_dialog = False
        self.relayed_dialog = False
        try:
            return self.route()
        except Exception as error:
            KSR.xlog.xwarn("nxs_route_unavailable category=" + type(error).__name__ + "\n")
            return self.deny(503, "Route unavailable")
        finally:
            if self.reserved_dialog and not self.relayed_dialog:
                self.release("dialogs")

    def transaction_key(self):
        via = KSR.pv.get("$hdr(Via)") or ""
        branch = re.search(r"(?:^|;)branch=(z9hG4bK[^; ,]+)", via)
        if branch is None:
            raise ValueError("missing transaction branch")
        components = (
            KSR.pv.get("$ci"),
            KSR.pv.get("$ft"),
            str(KSR.pv.get("$cs")),
            branch.group(1),
            via.split()[1].split(";")[0],
        )
        if any(not value or len(value.encode()) > 256 for value in components):
            raise ValueError("invalid transaction identity")
        return hashlib.sha256(json.dumps(components).encode()).hexdigest()

    def validate_dialog_sequence(self, method, reverse):
        key = KSR.pv.get("$dlg_var(protocol_key)")
        if not key:
            return False
        if method != "ACK" and KSR.tm.t_check_trans() == 0:
            return None
        KSR.htable.sht_lock("nxs_dialogs", key)
        try:
            variable = "$sht(nxs_dialogs=>" + key + ")"
            state = json.loads(KSR.pv.get(variable) or "{}")
            if state.get("owner") != KSR.pv.get("$dlg_var(protocol_owner)"):
                return False
            direction = "reverse" if reverse else "forward"
            sequence = int(KSR.pv.get("$cs"))
            if method == "ACK":
                return sequence == state.get(direction + "_invite")
            if sequence <= state.get(direction, 0):
                return False
            if method == "PRACK":
                reliable = KSR.pv.get("$dlg_var(reliable_sequence)")
                expected = f"{reliable} {state.get(direction + '_invite')} INVITE"
                if not reliable or KSR.pv.get("$hdr(RAck)") != expected:
                    return False
                KSR.pv.sets("$dlg_var(reliable_sequence)", "")
            state[direction] = sequence
            if method == "INVITE":
                state[direction + "_invite"] = sequence
            KSR.pv.sets(variable, json.dumps(state))
            return True
        finally:
            KSR.htable.sht_unlock("nxs_dialogs", key)

    def route(self):
        method = KSR.pv.get("$rm")
        source = ipaddress.ip_address(KSR.pv.get("$si"))
        transport = self.observed_transport()
        certificate = self.observed_certificate() if transport == "TLS" else None
        observation = {
            "source_address": str(source),
            "transport": transport,
            "certificate_sha256": certificate,
        }
        peers = [
            peer
            for peer in self.peers
            if source in ipaddress.ip_network(peer["network"])
            and peer.get("transport", "UDP") == transport
            and (transport != "TLS" or peer.get("certificate_sha256") == certificate)
        ]
        reverse = False
        if (
            KSR.pv.get("$tt")
            and KSR.dialog.dlg_get(KSR.pv.get("$ci"), KSR.pv.get("$ft"), KSR.pv.get("$tt")) > 0
        ):
            reverse = KSR.pv.get("$ft") != KSR.pv.get("$dlg_var(origin_tag)")
            if reverse:
                if (
                    str(source) != KSR.pv.get("$dlg_var(target_host)")
                    or transport != KSR.pv.get("$dlg_var(target_transport)")
                    or (
                        transport == "UDP"
                        and str(KSR.pv.get("$sp")) != KSR.pv.get("$dlg_var(target_port)")
                    )
                    or (
                        transport == "TLS"
                        and certificate != KSR.pv.get("$dlg_var(target_certificate)")
                    )
                ):
                    return self.deny()
                peers = [
                    peer for peer in self.peers if peer["id"] == KSR.pv.get("$dlg_var(peer_id)")
                ]
        if len(peers) != 1:
            return self.deny()
        peer = peers[0]
        if not self.rate_allowed(peer):
            return self.deny(503, "Rate exceeded")
        raw = KSR.pv.get("$mb")
        header_block = raw.split("\r\n\r\n", 1)[0]
        lines = header_block.split("\r\n")
        if (
            len(raw.encode()) > 65536
            or len(lines) > 101
            or any(len(line.encode()) > 2048 for line in lines)
        ):
            return self.deny(400, "Invalid request")
        if any(line.startswith((" ", "\t")) for line in lines):
            return self.deny(400, "Invalid request")
        lengths = [
            line.split(":", 1)[1].strip()
            for line in lines[1:]
            if line.split(":", 1)[0].lower() in {"content-length", "l"}
        ]
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            return self.deny(400, "Invalid length")
        body = raw.split("\r\n\r\n", 1)
        if len(body) != 2 or len(body[1].encode()) != int(lengths[0]):
            return self.deny(400, "Invalid length")
        if KSR.sanity.sanity_check(17895, 7) < 0:
            return self.deny(400, "Invalid request")
        if KSR.maxfwd.process_maxfwd(70) < 0:
            return self.deny(483, "Too Many Hops")
        if method not in {"INVITE", "ACK", "CANCEL", "BYE", "UPDATE", "PRACK", "INFO", "OPTIONS"}:
            return self.deny(405, "Method Not Allowed")
        permit_headers = [
            line.split(":", 1)[1].strip()
            for line in lines
            if line.lower().startswith("x-nxs-egress-permit:")
        ]
        KSR.textops.remove_hf_re("^X-(NXS-|Organization-ID|Cell-ID|Placement-Generation)")
        if method == "OPTIONS" and not KSR.pv.get("$tt"):
            if KSR.pv.get("$rd") not in self.edge_hosts or KSR.pv.get("$hdr(Route)"):
                return self.deny()
            KSR.sl.send_reply(200, "Edge alive")
            return 1
        transaction_key = self.transaction_key()
        transaction_owner = "$sht(nxs_transactions=>" + transaction_key + ")"
        if method in {"CANCEL", "ACK"} and KSR.pv.get(transaction_owner):
            if KSR.pv.get(transaction_owner) != peer["id"]:
                return self.deny()
            if KSR.tm.t_check_trans() > 0:
                KSR.tm.t_relay()
                return 1
        if method == "CANCEL":
            return self.deny(481, "No transaction")
        if KSR.pv.get("$tt"):
            if KSR.dialog.dlg_get(KSR.pv.get("$ci"), KSR.pv.get("$ft"), KSR.pv.get("$tt")) < 0:
                return self.deny(481, "Dialog not admitted")
            if KSR.pv.get("$dlg_var(peer_id)") != peer["id"]:
                return self.deny()
            if not reverse and (
                str(source) != KSR.pv.get("$dlg_var(origin_host)")
                or transport != KSR.pv.get("$dlg_var(origin_transport)")
                or (
                    transport == "UDP"
                    and str(KSR.pv.get("$sp")) != KSR.pv.get("$dlg_var(origin_port)")
                )
                or (
                    transport == "TLS" and certificate != KSR.pv.get("$dlg_var(origin_certificate)")
                )
            ):
                return self.deny()
            if KSR.rr.loose_route() < 0:
                return self.deny()
            prefix = "origin" if reverse else "target"
            pinned_host = KSR.pv.get("$dlg_var(" + prefix + "_host)")
            pinned_port = KSR.pv.get("$dlg_var(" + prefix + "_port)")
            pinned_transport = KSR.pv.get("$dlg_var(" + prefix + "_transport)")
            if KSR.pv.get("$rd") != pinned_host or str(KSR.pv.get("$rp")) != pinned_port:
                return self.deny()
            request_uri = KSR.pv.get("$ru")
            explicit_transport = re.search(r";transport=([a-z]+)(?:;|$)", request_uri)
            requested_transport = (
                explicit_transport.group(1).upper()
                if explicit_transport
                else ("TLS" if request_uri.startswith("sips:") else "UDP")
            )
            if requested_transport != pinned_transport or (
                request_uri.startswith("sips:") != (pinned_transport == "TLS")
            ):
                return self.deny()
            sequence_valid = self.validate_dialog_sequence(method, reverse)
            if sequence_valid is None:
                return 1
            if not sequence_valid:
                return self.deny(481, "Dialog sequence rejected")
            KSR.pv.sets("$du", KSR.pv.get("$dlg_var(" + prefix + "_uri)"))
            KSR.pv.seti("$avp(nxs_reverse)", 1 if reverse else 0)
            KSR.pv.seti("$avp(nxs_initial)", 0)
            KSR.pv.sets("$avp(nxs_protocol)", KSR.pv.get("$dlg_var(protocol_key)"))
            KSR.pv.sets("$avp(nxs_protocol_owner)", KSR.pv.get("$dlg_var(protocol_owner)"))
            KSR.pv.sets("$avp(nxs_reply_host)", pinned_host)
            KSR.pv.sets("$avp(nxs_reply_port)", pinned_port)
            KSR.pv.sets(
                "$avp(nxs_reply_transport)", KSR.pv.get("$dlg_var(" + prefix + "_transport)")
            )
            KSR.pv.sets(
                "$avp(nxs_reply_certificate)", KSR.pv.get("$dlg_var(" + prefix + "_certificate)")
            )
            KSR.tm.t_on_reply("ksr_route_reply")
            handle = KSR.pv.get("$dlg_var(route_handle)")
            if handle:
                KSR.pv.sets("$avp(nxs_handle)", handle)
                KSR.tm.t_on_reply("ksr_route_reply")
            KSR.tm.t_relay()
            return 1
        if method != "INVITE":
            return self.deny()
        if KSR.pv.get("$hdr(Route)") or KSR.pv.get("$hdr(Record-Route)"):
            return self.deny()
        contact = re.fullmatch(
            r"<(?P<uri>sips?:[A-Za-z0-9_.+~-]+@(?P<host>\[[a-fA-F0-9:]+\]|[0-9.]+):(?P<port>[0-9]{1,5})(?:;transport=(?P<transport>udp|tcp|tls))?)>",
            KSR.pv.get("$ct") or "",
        )
        if (
            contact is None
            or contact["host"].strip("[]") != str(source)
            or not 1024 <= int(contact["port"]) <= 65535
            or (contact["transport"] or "udp").upper() != transport
            or (transport == "UDP" and contact["port"] != str(KSR.pv.get("$sp")))
            or (transport != "UDP" and int(contact["port"]) != peer.get("contact_port"))
            or (
                transport == "TLS"
                and self.tls_targets.get((str(source), int(contact["port"]))) != certificate
            )
        ):
            return self.deny()
        if KSR.tmx.t_precheck_trans() > 0:
            if KSR.pv.get(transaction_owner) != peer["id"]:
                return self.deny()
            KSR.tm.t_check_trans()
            return 1
        if KSR.tm.t_check_trans() == 0:
            return 1
        called, host = KSR.pv.get("$rU"), KSR.pv.get("$rd")
        if peer["direction"] == "INBOUND":
            if (
                not re.fullmatch(r"\+[1-9][0-9]{6,14}", called or "")
                or host not in peer["ingress_hosts"]
                or not re.fullmatch(
                    ("sips" if transport == "TLS" else "sip")
                    + r":\+[1-9][0-9]{6,14}@[A-Za-z0-9.-]+(?::[0-9]{1,5})?"
                    + ("" if transport == "UDP" else ";transport=" + transport.lower()),
                    KSR.pv.get("$ru") or "",
                )
            ):
                return self.deny()
        elif peer["direction"] == "OUTBOUND":
            if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", called or "") or len(permit_headers) != 1:
                return self.deny()
            if host not in self.edge_hosts:
                return self.deny()
            called = "+" + called.lstrip("+")
        else:
            return self.deny()
        if not self.reserve("dialogs", self.limits["dialogs"]):
            return self.deny(503, "Dialog capacity exhausted")
        self.reserved_dialog = True
        via = KSR.pv.get("$hdr(Via)")
        branch = re.search(r"(?:^|;)branch=(z9hG4bK[^; ,]+)", via)
        if branch is None:
            return self.deny(400, "Invalid transaction")
        transaction = {
            "call_id": KSR.pv.get("$ci"),
            "from_tag": KSR.pv.get("$ft"),
            "cseq": int(KSR.pv.get("$cs")),
            "method": "INVITE",
            "via_branch": branch.group(1),
            "via_sent_by": via.split()[1].split(";")[0],
        }
        if peer["direction"] == "INBOUND":
            decision = self.resolver(
                "/internal/sip/inbound",
                {
                    "route": {
                        "peer_id": peer["id"],
                        "called_number": called,
                        "ingress_host": host,
                        "transaction": transaction,
                    },
                    "observed_peer": observation,
                },
            )
            target = decision["route"]
        else:
            decision = self.resolver(
                "/internal/sip/egress",
                {
                    "token": permit_headers[0],
                    "destination": called,
                    "transaction": transaction,
                    "peer_id": peer["id"],
                    "observed_peer": observation,
                },
            )
            if decision.get("initial_relay_granted") is not True:
                return self.deny(409, "Already issued")
            target = {
                "target_host": decision["host"],
                "target_port": decision["port"],
                "target_transport": decision["transport"],
                "certificate_sha256": decision.get("certificate_sha256"),
            }
        address = ipaddress.ip_address(target["target_host"])
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or (peer["direction"] == "INBOUND" and not address.is_private)
        ):
            return self.deny()
        target_transport = target["target_transport"]
        if target_transport not in {"UDP", "TCP", "TLS"}:
            return self.deny()
        if target_transport == "TLS" and (
            not target.get("certificate_sha256")
            or self.tls_targets.get((str(address), target["target_port"]))
            != target["certificate_sha256"]
        ):
            return self.deny()
        if peer["direction"] == "INBOUND":
            issued = self.resolver("/internal/sip/issue", {"handle": decision["handle"]})
            if issued.get("initial_relay_granted") is not True:
                return self.deny(409, "Already issued")
        KSR.rr.record_route()
        KSR.dialog.dlg_manage()
        protocol_key = hashlib.sha256(
            json.dumps((KSR.pv.get("$ci"), KSR.pv.get("$ft"))).encode()
        ).hexdigest()
        KSR.htable.sht_lock("nxs_dialogs", protocol_key)
        try:
            owner_key = "$sht(nxs_dialogs=>" + protocol_key + ")"
            if KSR.pv.get(owner_key):
                return self.deny(409, "Dialog identity already used")
            protocol_owner = str(
                target["route_id"] if peer["direction"] == "INBOUND" else decision["permit_id"]
            )
            KSR.pv.sets(
                owner_key,
                json.dumps(
                    {
                        "owner": protocol_owner,
                        "forward": int(KSR.pv.get("$cs")),
                        "forward_invite": int(KSR.pv.get("$cs")),
                    }
                ),
            )
            KSR.pv.sets("$dlg_var(protocol_key)", protocol_key)
            KSR.pv.sets("$dlg_var(protocol_owner)", protocol_owner)
            KSR.pv.sets("$avp(nxs_protocol)", protocol_key)
            KSR.pv.sets("$avp(nxs_protocol_owner)", protocol_owner)
            KSR.pv.seti("$avp(nxs_initial)", 1)
        finally:
            KSR.htable.sht_unlock("nxs_dialogs", protocol_key)
        KSR.pv.sets("$dlg_var(route_handle)", decision["handle"])
        KSR.pv.sets("$avp(nxs_handle)", decision["handle"])
        KSR.tm.t_on_reply("ksr_route_reply")
        target_uri = sip_destination(str(address), target["target_port"], target_transport)
        if peer["direction"] == "OUTBOUND":
            KSR.pv.sets(
                "$ru",
                sip_destination(str(address), target["target_port"], target_transport, called),
            )
        KSR.pv.sets("$dlg_var(peer_id)", peer["id"])
        KSR.pv.sets("$dlg_var(origin_tag)", KSR.pv.get("$ft"))
        KSR.pv.sets("$dlg_var(origin_host)", str(source))
        KSR.pv.sets("$dlg_var(origin_port)", contact["port"])
        KSR.pv.sets("$dlg_var(origin_uri)", contact["uri"])
        KSR.pv.sets("$dlg_var(target_host)", str(address))
        KSR.pv.sets("$dlg_var(target_port)", str(target["target_port"]))
        KSR.pv.sets("$dlg_var(target_transport)", target_transport)
        KSR.pv.sets("$dlg_var(target_certificate)", target.get("certificate_sha256") or "")
        KSR.pv.sets("$dlg_var(origin_transport)", transport)
        KSR.pv.sets("$dlg_var(origin_certificate)", certificate or "")
        KSR.pv.sets("$dlg_var(target_uri)", target_uri)
        KSR.pv.sets("$du", target_uri)
        KSR.pv.sets("$avp(nxs_reply_host)", str(address))
        KSR.pv.sets("$avp(nxs_reply_port)", str(target["target_port"]))
        KSR.pv.sets("$avp(nxs_reply_transport)", target_transport)
        KSR.pv.sets("$avp(nxs_reply_certificate)", target.get("certificate_sha256") or "")
        KSR.tm.t_on_reply("ksr_route_reply")
        KSR.pv.sets(transaction_owner, peer["id"])
        if KSR.tm.t_relay() < 0:
            return self.deny(503, "Target unavailable")
        self.relayed_dialog = True
        return 1


def mod_init():
    os.umask(0o077)
    return Edge()
