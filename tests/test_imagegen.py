import base64
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
import urllib.error
import zlib

SCRIPT = Path(__file__).resolve().parents[1] / "skills/codex-api-imagegen/scripts/imagegen.py"
SPEC = importlib.util.spec_from_file_location("imagegen", SCRIPT)
imagegen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(imagegen)
TEST_KEY = "test-local-api-credential-never-log"


def png(red=255):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0]))) + chunk(b"IEND", b""))


def image_item():
    return {"id": "ig_test", "type": "image_generation_call", "status": "completed",
            "result": base64.b64encode(png()).decode()}


@contextmanager
def fake_api(mode="sse", callback=None):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))})
            if callback:
                callback()
            if mode == "redirect":
                self.send_response(307)
                self.send_header("Location", "/credential-target")
                self.end_headers()
                return
            if mode == "error":
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": "Denied " + TEST_KEY + " Bearer alternate-secret",
                                                       "code": "upstream_unavailable"}}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if mode in {"json", "no_image", "failed_json"} else "text/event-stream")
            self.end_headers()
            response = {"id": "resp_test", "status": "completed", "output": [image_item()]}
            if mode == "no_image":
                response["output"] = [{"type": "message", "content": [{"type": "output_text", "text": "I generated it."}]}]
            if mode == "failed_json":
                response["status"] = "failed"
            if mode in {"json", "no_image", "failed_json"}:
                self.wfile.write(json.dumps(response).encode())
                return
            if mode == "malformed":
                self.wfile.write(b"data: {invalid-json}\n\n")
                return
            if mode == "invalid_image":
                response["output"][0]["result"] = base64.b64encode(b"not an image").decode()
            events = [{"type": "response.created", "response": {"id": "resp_test"}},
                      {"type": "response.output_item.done", "item": response["output"][0]}]
            if mode != "disconnect":
                events.append({"type": "response.completed", "response": response})
            try:
                for event in events:
                    self.wfile.write((": keepalive\n\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                # Response-limit tests deliberately close the client early.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def config(self, content):
        (self.home / "config.toml").write_text(content, encoding="utf-8")

    def test_inline_provider_credential_stays_in_memory(self):
        self.config(f'model_provider = "relay"\n[model_providers.relay]\nbase_url = "https://relay.example.test"\nexperimental_bearer_token = "{TEST_KEY}"\n')
        connection = imagegen.resolve_connection(self.home, environ={})
        self.assertEqual(connection["endpoint"], "https://relay.example.test/v1/responses")
        self.assertEqual(connection["headers"]["Authorization"], "Bearer " + TEST_KEY)

    def test_windows_bom_config_and_invalid_provider_shapes(self):
        content = f'model_provider="relay"\n[model_providers.relay]\nbase_url="https://relay.example.test"\nexperimental_bearer_token="{TEST_KEY}"\n'
        (self.home / "config.toml").write_text(content, encoding="utf-8-sig")
        self.assertEqual(imagegen.resolve_connection(self.home, environ={})["key_source"],
                         "provider.experimental_bearer_token")
        for content in ['model_provider=[]', 'model_providers="invalid"',
                        'model_provider="relay"\n[model_providers]\nrelay="invalid"']:
            self.config(content)
            with self.assertRaises(imagegen.SkillError):
                imagegen.resolve_connection(self.home, environ={})

    def test_malformed_url_has_a_readable_error(self):
        for base in ["https://[invalid", "https://relay.example.test:invalid"]:
            with self.assertRaises(imagegen.SkillError):
                imagegen.responses_url(base)

    def test_profile_selects_its_own_provider_and_environment_key(self):
        self.config('model_provider="unused"\nprofile="images"\n[profiles.images]\nmodel_provider="relay"\n[model_providers.relay]\nbase_url="https://relay.example.test/custom/v1/"\nenv_key="RELAY_TEST_KEY"\n')
        c = imagegen.resolve_connection(self.home, environ={"RELAY_TEST_KEY": TEST_KEY})
        self.assertEqual(c["provider"], "relay")
        self.assertEqual(c["endpoint"], "https://relay.example.test/custom/v1/responses")
        with self.assertRaises(imagegen.SkillError):
            imagegen.resolve_connection(self.home, environ={"OPENAI_API_KEY": "unrelated-provider-key"})

    def test_explicit_profile_overrides_config_profile(self):
        self.config('profile="missing"\n[profiles.images]\nmodel_provider="openai"\n')
        c = imagegen.resolve_connection(self.home, "images", {"OPENAI_API_KEY": TEST_KEY})
        self.assertEqual(c["profile"], "images")

    def test_codex_home_environment_is_used(self):
        self.config('model_provider="openai"\n')
        c = imagegen.resolve_connection(environ={"CODEX_HOME": str(self.home), "OPENAI_API_KEY": TEST_KEY})
        self.assertEqual(c["key_source"], "environment.OPENAI_API_KEY")

    def test_api_key_auth_file_and_oauth_not_confused(self):
        self.config('model_provider="openai"\n')
        auth = self.home / "auth.json"
        auth.write_text(json.dumps({"OPENAI_API_KEY": TEST_KEY}), encoding="utf-8")
        self.assertEqual(imagegen.resolve_connection(self.home, environ={})["key_source"], "auth.json.OPENAI_API_KEY")
        auth.write_text(json.dumps({"tokens": {"access_token": "not-an-api-key"}}), encoding="utf-8")
        with self.assertRaisesRegex(imagegen.SkillError, "ChatGPT login"):
            imagegen.resolve_connection(self.home, environ={})

    def test_custom_provider_does_not_borrow_an_unrelated_openai_key(self):
        self.config('model_provider="relay"\n[model_providers.relay]\nbase_url="https://relay.example.test/v1"\n')
        with self.assertRaises(imagegen.SkillError):
            imagegen.resolve_connection(self.home, environ={"OPENAI_API_KEY": TEST_KEY})

    def test_environment_headers_and_reserved_header_rejection(self):
        self.config(f'model_provider="relay"\n[model_providers.relay]\nbase_url="https://relay.example.test/v1"\nexperimental_bearer_token="{TEST_KEY}"\n[model_providers.relay.env_http_headers]\nX-Test="HEADER_TEST"\n')
        c = imagegen.resolve_connection(self.home, environ={"HEADER_TEST": "header-secret"})
        self.assertEqual(c["headers"]["X-Test"], "header-secret")
        self.assertNotIn("header-secret", imagegen.redact("header-secret", c["secrets"]))
        with self.assertRaises(imagegen.SkillError):
            imagegen.resolve_connection(self.home, environ={})

    def test_unsafe_urls_are_rejected(self):
        for url in ["http://remote.example.test", "https://user:pass@example.test", "https://example.test?api_key=secret", "file:///secret"]:
            with self.subTest(url=url), self.assertRaises(imagegen.SkillError):
                imagegen.responses_url(url)
        self.assertEqual(imagegen.responses_url("https://example.test/v1/responses"), "https://example.test/v1/responses")

    def test_reference_images_keep_content_and_order(self):
        one, two = self.home / "门.png", self.home / "reference two.png"
        one.write_bytes(png(200))
        two.write_bytes(png(100))
        payload = imagegen.make_payload("只添加‘星河科技’", [one, two])
        self.assertEqual(payload["tools"][0]["action"], "edit")
        content = payload["input"][0]["content"]
        self.assertEqual(base64.b64decode(content[1]["image_url"].split(",")[1]), one.read_bytes())
        self.assertEqual(base64.b64decode(content[2]["image_url"].split(",")[1]), two.read_bytes())
        self.assertEqual(payload["model"], "gpt-6.1-sol")
        self.assertEqual(payload["tools"][0]["model"], "gpt-image-2")

    def test_truncated_and_corrupt_png_are_rejected(self):
        for data in [png()[:-5], png()[:32] + b"bad" + png()[35:]]:
            with self.assertRaises(imagegen.SkillError):
                imagegen.image_info(data)


class TransportFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "image.png"
        self.connection = {"endpoint": "https://relay.example.test/v1/responses",
                           "headers": {"Authorization": "Bearer " + TEST_KEY}, "secrets": [TEST_KEY],
                           "dns_mode": "system"}

    def failed_open(self, error):
        with patch.object(imagegen.urllib.request, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(imagegen.SkillError) as raised:
                imagegen.generate(self.connection, imagegen.make_payload("test"), self.out)
            factory.return_value.open.assert_called_once()
        self.assertFalse(self.out.exists())
        message = str(raised.exception)
        self.assertNotIn(TEST_KEY, message)
        self.assertNotIn("relay.example.test", message)
        return message

    def test_dns_failure_is_known_to_precede_submission(self):
        message = self.failed_open(urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, TEST_KEY)))
        self.assertIn("DNS lookup failed", message)
        self.assertIn("No image-generation request was sent", message)
        self.assertIn("network permission", message)
        self.assertNotIn("uncertain", message)

    def test_dns_failure_through_urlopen_never_opens_a_connection(self):
        with patch.object(socket, "getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, TEST_KEY)) as dns, \
                patch.object(socket.socket, "connect") as connect:
            with self.assertRaisesRegex(imagegen.SkillError, "No image-generation request was sent") as raised:
                imagegen.generate(self.connection, imagegen.make_payload("test"), self.out)
            dns.assert_called_once()
            connect.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertNotIn(TEST_KEY, str(raised.exception))

    def test_denied_connection_identifies_permission_requirement(self):
        message = self.failed_open(urllib.error.URLError(PermissionError(1, TEST_KEY)))
        self.assertIn("local permissions", message)
        self.assertIn("No image-generation request was sent", message)

    def test_refused_connection_does_not_claim_a_paid_submission(self):
        message = self.failed_open(urllib.error.URLError(ConnectionRefusedError(TEST_KEY)))
        self.assertIn("connection was refused", message)
        self.assertIn("No image-generation request was sent", message)

    def test_certificate_failure_keeps_verification_enabled(self):
        message = self.failed_open(urllib.error.URLError(ssl.SSLCertVerificationError(1, TEST_KEY)))
        self.assertIn("before the image-generation request was sent", message)
        self.assertIn("do not disable certificate verification", message)

    def test_timeout_and_unknown_transport_never_claim_safe_resubmission(self):
        for error in [TimeoutError(TEST_KEY), urllib.error.URLError(TimeoutError(TEST_KEY)),
                      urllib.error.URLError(TEST_KEY), OSError(TEST_KEY),
                      urllib.error.URLError(ssl.SSLError(1, TEST_KEY))]:
            with self.subTest(error=type(error).__name__):
                message = self.failed_open(error)
                self.assertIn("uncertain", message)
                self.assertIn("do not automatically resubmit", message)
                self.assertNotIn("No image-generation request was sent", message)

    def test_failure_after_headers_cannot_be_called_unsubmitted(self):
        with patch.object(imagegen.urllib.request, "build_opener") as factory:
            response = factory.return_value.open.return_value.__enter__.return_value
            response.headers = {"Content-Type": "text/event-stream"}
            response.readline.side_effect = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, TEST_KEY))
            with self.assertRaisesRegex(imagegen.SkillError, "uncertain") as raised:
                imagegen.generate(self.connection, imagegen.make_payload("test"), self.out)
            factory.return_value.open.assert_called_once()
        self.assertNotIn("No image-generation request was sent", str(raised.exception))
        self.assertNotIn(TEST_KEY, str(raised.exception))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="imagegen tests ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.out = self.home / "output" / "门牌.png"

    def run_cli(self, base, *args):
        (self.home / "config.toml").write_text(f'model_provider="relay"\n[model_providers.relay]\nbase_url="{base}"\nexperimental_bearer_token="{TEST_KEY}"\n', encoding="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--codex-home", str(self.home)],
                              cwd=self.home, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def generate(self, base):
        return self.run_cli(base, "generate", "--prompt", "白底红苹果", "--out", str(self.out))

    def assert_clean(self, result):
        self.assertNotIn(TEST_KEY, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_sse_generation_deduplicates_and_records_actual_dimensions(self):
        with fake_api() as (base, received):
            result = self.generate(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_bytes(), png())
        self.assertEqual(len(list(self.out.parent.glob("*.png"))), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["path"], "/v1/responses")
        self.assertEqual(received[0]["body"]["model"], "gpt-6.1-sol")
        self.assertEqual(received[0]["headers"]["Authorization"], "Bearer " + TEST_KEY)
        summary = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(summary["images"][0]["width"], 1)
        self.assert_clean(result)

    def test_windows_legacy_pipe_encoding_keeps_chinese_output(self):
        with patch.dict(os.environ, {"PYTHONIOENCODING": "cp1252"}), fake_api() as (base, received):
            result = self.generate(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_bytes(), png())
        self.assertIn("门牌.png", result.stdout)

    def test_non_streaming_json_is_supported(self):
        with fake_api("json") as (base, received):
            result = self.generate(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_bytes(), png())

    def test_edit_with_prompt_file_and_unicode_paths(self):
        reference = self.home / "原始 图片.png"
        reference.write_bytes(png())
        prompt = self.home / "修改要求.txt"
        prompt.write_text('只加上“星河科技”，保留门的样子。', encoding="utf-8")
        with fake_api() as (base, received):
            result = self.run_cli(base, "edit", "--image", str(reference), "--prompt-file", str(prompt), "--out", str(self.out))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = received[0]["body"]
        self.assertEqual(body["tools"][0]["action"], "edit")
        self.assertEqual(body["input"][0]["content"][0]["text"], prompt.read_text(encoding="utf-8"))

    def test_plain_text_is_not_success(self):
        with fake_api("no_image") as (base, received):
            result = self.generate(base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no image file", result.stderr)
        self.assertFalse(self.out.exists())
        self.assert_clean(result)

    def test_failed_json_status_is_not_success(self):
        with fake_api("failed_json") as (base, received):
            result = self.generate(base)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.out.exists())

    def test_disconnect_preserves_image_without_success_or_retry(self):
        with fake_api("disconnect") as (base, received):
            result = self.generate(base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(self.out.read_bytes(), png())
        self.assertIn("uncertain", result.stderr)
        saved = json.loads(result.stdout.splitlines()[0])["saved_image"]
        self.assertEqual(Path(saved["path"]).resolve(), self.out.resolve())
        self.assert_clean(result)

    def test_redirect_does_not_forward_authentication(self):
        with fake_api("redirect") as (base, received):
            result = self.generate(base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(received), 1)
        self.assertIn("redirect refused", result.stderr)
        self.assert_clean(result)

    def test_server_error_is_redacted_and_never_retried(self):
        with fake_api("error") as (base, received):
            result = self.generate(base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(received), 1)
        self.assertIn("503", result.stderr)
        self.assertNotIn("alternate-secret", result.stderr)
        self.assert_clean(result)

    def test_existing_output_stops_before_api_call(self):
        self.out.parent.mkdir()
        self.out.write_bytes(b"old")
        with fake_api() as (base, received):
            result = self.generate(base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(received, [])
        self.assertEqual(self.out.read_bytes(), b"old")

    def test_file_created_during_request_is_preserved(self):
        with fake_api(callback=lambda: self.out.write_bytes(b"other writer")) as (base, received):
            result = self.generate(base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_bytes(), b"other writer")
        recovered = list(self.out.parent.glob("*-recovered-*.png"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), png())

    def test_malformed_responses_are_errors_without_retry(self):
        for mode in ["malformed", "invalid_image"]:
            with self.subTest(mode=mode), fake_api(mode) as (base, received):
                result = self.generate(base)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len(received), 1)
            self.assertFalse(self.out.exists())
            self.assert_clean(result)

    def test_response_size_limits_fail_safely(self):
        connection = {"endpoint": "", "headers": {}, "secrets": []}
        with fake_api() as (base, received), patch.object(imagegen, "MAX_RESPONSE", 20):
            connection["endpoint"] = base + "/v1/responses"
            self.out.parent.mkdir()
            with self.assertRaisesRegex(imagegen.SkillError, "limits"):
                imagegen.generate(connection, imagegen.make_payload("test"), self.out)
        self.assertEqual(len(received), 1)

    def test_local_save_failure_is_distinct_from_connection_permission(self):
        with fake_api() as (base, received), patch.object(imagegen, "save_image", side_effect=PermissionError(TEST_KEY)):
            connection = {"endpoint": base + "/v1/responses", "headers": {}, "secrets": [TEST_KEY]}
            with self.assertRaises(imagegen.SkillError) as raised:
                imagegen.generate(connection, imagegen.make_payload("test"), self.out)
        self.assertEqual(len(received), 1)
        message = str(raised.exception)
        self.assertIn("file I/O failed", message)
        self.assertIn("uncertain", message)
        self.assertNotIn("No image-generation request was sent", message)
        self.assertNotIn(TEST_KEY, message)

    def test_inspect_does_not_make_an_api_request_or_print_key(self):
        with fake_api() as (base, received):
            result = self.run_cli(base, "inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(received, [])
        self.assertEqual(json.loads(result.stdout)["key_source"], "provider.experimental_bearer_token")
        self.assert_clean(result)


if __name__ == "__main__":
    unittest.main()
