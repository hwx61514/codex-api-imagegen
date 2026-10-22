import io
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_imagegen import imagegen, image_item, TEST_KEY, png


def dns_reply(name="api.example.com", rrtype=1, address="1.1.1.1"):
    return {"Status": 0, "Answer": [{"name": name, "type": rrtype, "data": address}]}


def response_socket(body, status=200):
    sock = Mock()
    sock.makefile.return_value = io.BytesIO(
        f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
    return sock


class DNSLookupTests(unittest.TestCase):
    def test_local_names_and_ip_literals_never_leave_system_resolver(self):
        for host in ["localhost", "relay", "relay.local", "relay.internal", "relay.lan",
                     "relay.home", "relay.corp", "relay.test", "relay.invalid", "relay.onion",
                     "127.0.0.1", "192.168.1.1", "::1", "1.1.1.1", "relay.example"]:
            with self.subTest(host=host), patch.object(imagegen, "query_dns_resolver") as query:
                self.assertEqual(imagegen.fallback_dns(host), ([], None))
                query.assert_not_called()
        self.assertEqual(imagegen.public_dns_hostname("API.Example.COM."), "api.example.com")

    def test_system_mode_performs_no_external_lookup(self):
        with patch.object(imagegen, "query_dns_resolver") as query:
            self.assertEqual(imagegen.fallback_dns("api.example.com", "system"), ([], None))
            query.assert_not_called()

    def test_selected_region_uses_only_its_resolver(self):
        for mode in ("cn", "global"):
            with self.subTest(mode=mode), patch.object(imagegen, "query_dns_resolver", return_value=["1.1.1.1"]) as query:
                self.assertEqual(imagegen.fallback_dns("api.example.com", mode), (["1.1.1.1"], mode))
                self.assertEqual(query.call_args.args[:2], (mode, "api.example.com"))
                query.assert_called_once()

    def test_auto_returns_without_waiting_for_slow_region(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def lookup(source, hostname, deadline):
            if source == "cn":
                started.set()
                release.wait(3)
                finished.set()
                return []
            if not started.wait(2):
                raise AssertionError("Parallel lookup did not start")
            return ["1.1.1.1"]

        try:
            with patch.object(imagegen, "query_dns_resolver", side_effect=lookup):
                self.assertEqual(imagegen.fallback_dns("api.example.com"), (["1.1.1.1"], "global"))
                self.assertFalse(finished.is_set())
        finally:
            release.set()
            finished.wait(3)

    def test_failed_region_does_not_prevent_other_region_success(self):
        def lookup(source, hostname, deadline):
            if source == "cn":
                raise OSError("unreachable")
            return ["1.1.1.1"]

        with patch.object(imagegen, "query_dns_resolver", side_effect=lookup):
            self.assertEqual(imagegen.fallback_dns("api.example.com"), (["1.1.1.1"], "global"))

    def test_deadline_returns_failure_without_waiting_for_workers(self):
        release = threading.Event()
        finished = [threading.Event(), threading.Event()]

        def lookup(source, hostname, deadline):
            release.wait(3)
            finished[source == "global"].set()
            return []

        try:
            with patch.object(imagegen, "query_dns_resolver", side_effect=lookup):
                self.assertEqual(imagegen.fallback_dns("api.example.com", budget=0.02), ([], None))
                self.assertFalse(any(event.is_set() for event in finished))
        finally:
            release.set()
            for event in finished:
                event.wait(3)

    def test_addresses_follow_cname_and_accept_both_ip_families(self):
        data = {"Status": 0, "Answer": [
            {"name": "api.example.com.", "type": 5, "data": "target.example.com."},
            {"name": "target.example.com.", "type": 1, "data": "1.1.1.1"},
            {"name": "target.example.com.", "type": 28, "data": "2606:4700:4700::1111"},
            {"name": "target.example.com.", "type": 1, "data": "1.1.1.1"}]}
        self.assertEqual(imagegen.dns_addresses(data, "api.example.com", 1), ["1.1.1.1"])
        self.assertEqual(imagegen.dns_addresses(data, "api.example.com", 28), ["2606:4700:4700::1111"])

    def test_invalid_unrelated_private_and_multicast_answers_are_rejected(self):
        for address in ["127.0.0.1", "10.0.0.1", "192.168.1.1", "100.64.0.1", "169.254.1.1",
                        "224.0.0.1", "::1", "fc00::1", "ff02::1", "bad", 16843009, None, []]:
            for rrtype in (1, 28):
                with self.subTest(address=address, rrtype=rrtype):
                    self.assertEqual(imagegen.dns_addresses(dns_reply(rrtype=rrtype, address=address), "api.example.com", rrtype), [])
        for data in [{"Status": 3}, {"Status": 0, "Answer": "invalid"},
                     dns_reply(name="unrelated.example.com"), {"Status": 0, "Answer": [None]},
                     {"Status": 0, "Answer": dns_reply()["Answer"] * 129}]:
            self.assertEqual(imagegen.dns_addresses(data, "api.example.com", 1), [])

    def test_bootstrap_retains_dns_server_tls_and_sends_only_dns_query(self):
        for source in ("cn", "global"):
            socks = [response_socket(json.dumps(dns_reply(rrtype=1)).encode()),
                     response_socket(json.dumps(dns_reply(rrtype=28, address="2606:4700:4700::1111")).encode())]
            tls_hosts = []

            def wrap(context, sock, **kwargs):
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                tls_hosts.append(kwargs["server_hostname"])
                return sock

            with self.subTest(source=source), patch.object(socket, "create_connection", side_effect=socks) as connect, \
                    patch.object(ssl.SSLContext, "wrap_socket", new=wrap):
                result = imagegen.query_dns_resolver(source, "api.example.com", time.monotonic() + 4)
            self.assertEqual(result, ["1.1.1.1", "2606:4700:4700::1111"])
            server, path, ips = imagegen.DNS_RESOLVERS[source]
            self.assertEqual(tls_hosts, [server, server])
            self.assertEqual([call.args[0] for call in connect.call_args_list], [(ips[0], 443)] * 2)
            for rrtype, sock in zip((1, 28), socks):
                sent = b"".join(call.args[0] for call in sock.sendall.call_args_list).decode()
                self.assertIn(f"GET {path}?name=api.example.com&type={rrtype} HTTP/1.1", sent)
                self.assertIn(f"Host: {server}", sent)
                self.assertIn("Accept: application/dns-json", sent)
                self.assertNotIn("Authorization", sent)
                self.assertNotIn(TEST_KEY, sent)
                sock.close.assert_called()

    def test_dns_service_errors_and_oversized_responses_return_no_addresses(self):
        for body, status in [(b"not json", 200), (b"{}", 503), (b"x" * 65537, 200)]:
            socks = [response_socket(body, status) for _ in range(2)]
            with self.subTest(status=status, size=len(body)), \
                    patch.object(socket, "create_connection", side_effect=socks), \
                    patch.object(ssl.SSLContext, "wrap_socket", new=lambda context, sock, **kw: sock):
                self.assertEqual(imagegen.query_dns_resolver("cn", "api.example.com", time.monotonic() + 4), [])


class DNSConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "image.png"
        self.connection = {"endpoint": "https://api.example.com/v1/responses", "secrets": [TEST_KEY],
                           "headers": {"Authorization": "Bearer " + TEST_KEY}}
        body = json.dumps({"id": "resp_test", "status": "completed", "output": [image_item()]}).encode()
        self.sock = response_socket(body)
        self.tls = []
        self.env = patch.dict(os.environ, {"NO_PROXY": "*"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def wrap(self, context, sock, **kwargs):
        self.tls.append((context, kwargs["server_hostname"]))
        return sock

    def generate(self):
        return imagegen.generate(self.connection, imagegen.make_payload("test"), self.out)

    def test_normal_dns_does_not_contact_public_resolvers(self):
        with patch.object(socket, "create_connection", return_value=self.sock) as connect, \
                patch.object(ssl.SSLContext, "wrap_socket", new=lambda context, sock, **kw: self.wrap(context, sock, **kw)), \
                patch.object(imagegen, "fallback_dns") as lookup:
            self.assertEqual(self.generate()["status"], "completed")
        lookup.assert_not_called()
        connect.assert_called_once()
        self.assertEqual(self.out.read_bytes(), png())

    def test_failed_system_dns_uses_ip_but_preserves_api_host_tls_and_one_post(self):
        with patch.object(socket, "create_connection", side_effect=[socket.gaierror(), self.sock]) as connect, \
                patch.object(ssl.SSLContext, "wrap_socket", new=lambda context, sock, **kw: self.wrap(context, sock, **kw)), \
                patch.object(imagegen, "fallback_dns", return_value=(["1.1.1.1"], "cn")) as lookup:
            self.assertEqual(self.generate()["status"], "completed")
        lookup.assert_called_once_with("api.example.com", "auto")
        self.assertEqual([call.args[0] for call in connect.call_args_list], [("api.example.com", 443), ("1.1.1.1", 443)])
        context, host = self.tls[0]
        self.assertEqual(host, "api.example.com")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        sent = b"".join(call.args[0] for call in self.sock.sendall.call_args_list)
        self.assertEqual(sent.count(b"POST /v1/responses HTTP/1.1"), 1)
        self.assertIn(b"Host: api.example.com", sent)
        self.assertNotIn(b"Host: 1.1.1.1", sent)
        self.assertEqual(sent.count(TEST_KEY.encode()), 1)
        self.assertEqual(self.out.read_bytes(), png())

    def test_all_dns_sources_fail_without_sending_post(self):
        with patch.object(socket, "create_connection", side_effect=socket.gaierror()) as connect, \
                patch.object(imagegen, "fallback_dns", return_value=([], None)) as lookup:
            with self.assertRaisesRegex(imagegen.SkillError, "No image-generation request was sent"):
                self.generate()
        lookup.assert_called_once()
        connect.assert_called_once()
        self.sock.sendall.assert_not_called()

    def test_unreachable_ipv4_can_fall_through_to_ipv6_before_post(self):
        with patch.object(socket, "create_connection", side_effect=[socket.gaierror(), TimeoutError(), self.sock]) as connect, \
                patch.object(ssl.SSLContext, "wrap_socket", new=lambda context, sock, **kw: self.wrap(context, sock, **kw)), \
                patch.object(imagegen, "fallback_dns", return_value=(["1.1.1.1", "2606:4700:4700::1111"], "global")):
            self.assertEqual(self.generate()["status"], "completed")
        self.assertEqual(connect.call_args.args[0], ("2606:4700:4700::1111", 443))
        sent = b"".join(call.args[0] for call in self.sock.sendall.call_args_list)
        self.assertEqual(sent.count(b"POST /v1/responses HTTP/1.1"), 1)

    def test_api_failure_after_dns_recovery_does_not_resolve_or_submit_again(self):
        for body, status in [(b'{}', 503), (b'bad-json', 200)]:
            sock = response_socket(body, status)
            with self.subTest(status=status), \
                    patch.object(socket, "create_connection", side_effect=[socket.gaierror(), sock]) as connect, \
                    patch.object(ssl.SSLContext, "wrap_socket", new=lambda context, sock, **kw: self.wrap(context, sock, **kw)), \
                    patch.object(imagegen, "fallback_dns", return_value=(["1.1.1.1"], "cn")) as lookup:
                with self.assertRaises(imagegen.SkillError):
                    self.generate()
            self.assertEqual(connect.call_count, 2)
            lookup.assert_called_once()
            sent = b"".join(call.args[0] for call in sock.sendall.call_args_list)
            self.assertEqual(sent.count(b"POST /v1/responses HTTP/1.1"), 1)

    def test_system_mode_disables_fallback_in_actual_https_path(self):
        self.connection["dns_mode"] = "system"
        with patch.object(socket, "create_connection", side_effect=socket.gaierror()), \
                patch.object(imagegen, "fallback_dns") as lookup:
            with self.assertRaisesRegex(imagegen.SkillError, "No image-generation request was sent"):
                self.generate()
        lookup.assert_not_called()

    def test_non_dns_connect_failures_do_not_trigger_dns_or_post(self):
        for failure in [PermissionError(), TimeoutError(), ConnectionRefusedError()]:
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(socket, "create_connection", side_effect=failure) as connect, \
                    patch.object(imagegen, "fallback_dns") as lookup:
                with self.assertRaises(imagegen.SkillError):
                    self.generate()
                lookup.assert_not_called()
                connect.assert_called_once()
                self.sock.sendall.assert_not_called()

    def test_certificate_failure_does_not_trigger_dns_or_post(self):
        with patch.object(socket, "create_connection", return_value=self.sock), \
                patch.object(ssl.SSLContext, "wrap_socket", side_effect=ssl.SSLCertVerificationError()), \
                patch.object(imagegen, "fallback_dns") as lookup:
            with self.assertRaisesRegex(imagegen.SkillError, "certificate verification failed"):
                self.generate()
        lookup.assert_not_called()
        self.sock.sendall.assert_not_called()

    def test_failed_proxy_dns_is_not_replaced_by_direct_provider_connection(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.net:8080", "NO_PROXY": ""}, clear=True), \
                patch.object(socket, "create_connection", side_effect=socket.gaierror()) as connect, \
                patch.object(imagegen, "fallback_dns") as lookup:
            with self.assertRaisesRegex(imagegen.SkillError, "No image-generation request was sent"):
                self.generate()
        lookup.assert_not_called()
        self.assertEqual(connect.call_args.args[0], ("proxy.example.net", 8080))

    def test_no_proxy_retains_direct_provider_fallback(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.net:8080", "NO_PROXY": "api.example.com"}, clear=True), \
                patch.object(socket, "create_connection", side_effect=[socket.gaierror(), self.sock]) as connect, \
                patch.object(ssl.SSLContext, "wrap_socket", new=lambda context, sock, **kw: self.wrap(context, sock, **kw)), \
                patch.object(imagegen, "fallback_dns", return_value=(["1.1.1.1"], "global")) as lookup:
            self.assertEqual(self.generate()["status"], "completed")
        self.assertEqual(connect.call_args_list[0].args[0], ("api.example.com", 443))
        lookup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
