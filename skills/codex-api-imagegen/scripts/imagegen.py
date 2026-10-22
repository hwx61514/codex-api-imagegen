#!/usr/bin/env python3
"""Generate/edit images using the active Codex API provider; Python 3.11+."""

import argparse
import base64
import binascii
import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zlib

DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
MAX_IMAGE = 32 * 1024 * 1024
MAX_EVENT = 48 * 1024 * 1024
MAX_RESPONSE = 192 * 1024 * 1024
DNS_BUDGET = 4
DNS_RESOLVERS = {
    "cn": ("dns.alidns.com", "/resolve", ("223.5.5.5", "2400:3200::1")),
    "global": ("cloudflare-dns.com", "/dns-query", ("1.1.1.1", "2606:4700:4700::1111")),
}


class SkillError(Exception):
    pass


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        raise SkillError(f"Cannot read valid JSON from {path.name}.") from None


def resolve_connection(codex_home=None, profile=None, environ=None):
    env = os.environ if environ is None else environ
    home = Path(codex_home or env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    config_path = home / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SkillError("Cannot read valid Codex config.toml; set --codex-home if needed.") from None
    selected_profile = profile or config.get("profile")
    effective = dict(config)
    if selected_profile:
        profiles = config.get("profiles", {})
        if not isinstance(profiles, dict) or selected_profile not in profiles:
            raise SkillError("Selected Codex profile does not exist.")
        if not isinstance(profiles[selected_profile], dict):
            raise SkillError("Selected Codex profile must be a TOML table.")
        effective.update(profiles[selected_profile])
    provider_name = effective.get("model_provider", "openai")
    providers = config.get("model_providers", {})
    if not isinstance(provider_name, str) or not isinstance(providers, dict):
        raise SkillError("Invalid Codex model provider configuration.")
    provider = providers.get(provider_name)
    if provider is None:
        if provider_name != "openai":
            raise SkillError("Active Codex provider is not defined in model_providers.")
        provider = {}
    if not isinstance(provider, dict):
        raise SkillError("Active Codex provider must be a TOML table.")
    base = provider.get("base_url")
    if not base and provider_name == "openai":
        base = env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    if not base:
        raise SkillError("Active Codex provider has no base_url.")
    endpoint = responses_url(base)
    key = None
    source = None
    env_key = provider.get("env_key")
    if env_key:
        key = env.get(env_key)
        source = "provider.env_key"
        if not key:
            raise SkillError("The active provider's env_key variable is not set.")
    elif provider.get("experimental_bearer_token"):
        key = provider["experimental_bearer_token"]
        source = "provider.experimental_bearer_token"
    elif provider_name == "openai" or provider.get("requires_openai_auth"):
        key = env.get("OPENAI_API_KEY")
        source = "environment.OPENAI_API_KEY"
        if not key and (home / "auth.json").exists():
            key = read_json(home / "auth.json").get("OPENAI_API_KEY")
            source = "auth.json.OPENAI_API_KEY"
    if not isinstance(key, str) or not key.strip():
        raise SkillError("No API key found for the active Codex provider. ChatGPT login tokens are not API keys.")
    if "\n" in key or "\r" in key:
        raise SkillError("Invalid API key configuration.")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "text/event-stream"}
    sensitive = [key]
    for field, is_env in [("http_headers", False), ("env_http_headers", True)]:
        for name, value in provider.get(field, {}).items():
            if name.lower() in {"authorization", "host", "content-length", "content-type", "accept"}:
                raise SkillError("Reserved custom HTTP header is unsupported; use the provider API-key fields.")
            resolved = env.get(value) if is_env else value
            if not isinstance(resolved, str) or "\n" in resolved or "\r" in resolved or "\n" in name or "\r" in name:
                raise SkillError("Missing or invalid custom provider HTTP header.")
            headers[name] = resolved
            if resolved:
                sensitive.append(resolved)
    return {"endpoint": endpoint, "headers": headers, "secrets": sensitive,
            "provider": provider_name, "profile": selected_profile, "key_source": source}


def responses_url(base):
    if not isinstance(base, str):
        raise SkillError("Invalid provider base_url.")
    try:
        url = urllib.parse.urlsplit(base)
        url.port
    except ValueError:
        raise SkillError("Invalid provider base_url.") from None
    if not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
        raise SkillError("Provider base_url must not contain URL credentials, a query, or a fragment.")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise SkillError("Use HTTPS for the API provider; HTTP is allowed only for loopback tests.")
    path = url.path.rstrip("/")
    if not path:
        path = "/v1"
    if not path.endswith("/responses"):
        path += "/responses"
    return urllib.parse.urlunsplit((url.scheme, url.netloc, path, "", ""))


def image_info(data):
    if not data or len(data) > MAX_IMAGE:
        raise SkillError("Image is empty or exceeds the 32 MiB limit.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        pos, width, height, ended, pixels = 8, None, None, False, False
        while pos + 12 <= len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            end = pos + 12 + length
            if end > len(data):
                break
            kind = data[pos + 4:pos + 8]
            chunk = data[pos + 8:pos + 8 + length]
            crc = struct.unpack(">I", data[pos + 8 + length:end])[0]
            if zlib.crc32(kind + chunk) & 0xffffffff != crc:
                raise SkillError("PNG image checksum is invalid.")
            if pos == 8 and (kind != b"IHDR" or length != 13):
                raise SkillError("Invalid PNG header.")
            if kind == b"IHDR" and length == 13:
                width, height = struct.unpack(">II", chunk[:8])
            if kind == b"IDAT":
                pixels = True
            if kind == b"IEND":
                ended = length == 0 and end == len(data)
                break
            pos = end
        if not ended or not pixels or not width or not height or max(width, height) > 16384:
            raise SkillError("PNG image is truncated or has invalid dimensions.")
        return {"format": "png", "mime": "image/png", "width": width, "height": height}
    if data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        # JPEG has variable marker layouts; dimensions are reported by the viewer.
        return {"format": "jpeg", "mime": "image/jpeg"}
    if (len(data) >= 20 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
            and struct.unpack("<I", data[4:8])[0] + 8 == len(data)
            and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}):
        return {"format": "webp", "mime": "image/webp"}
    raise SkillError("The result is not a recognized PNG, JPEG, or WebP image.")


def make_payload(prompt, images=(), model=DEFAULT_MODEL, image_model=DEFAULT_IMAGE_MODEL,
                 size="auto", quality="medium", output_format="png"):
    if not prompt.strip():
        raise SkillError("Prompt must not be empty.")
    if len(images) > 3:
        raise SkillError("Use at most three reference images per request.")
    content = [{"type": "input_text", "text": prompt}]
    for path in images:
        try:
            with Path(path).open("rb") as file:
                data = file.read(MAX_IMAGE + 1)
        except OSError:
            raise SkillError("Cannot read a reference image.") from None
        info = image_info(data)
        content.append({"type": "input_image", "image_url": "data:" + info["mime"] + ";base64," + base64.b64encode(data).decode("ascii")})
    return {"model": model, "store": False, "stream": True,
            "instructions": "Call the image_generation tool exactly once to fulfill the user's image request. Return the generated image; text alone is not a completed image request.",
            "input": [{"role": "user", "content": content}],
            "tools": [{"type": "image_generation", "model": image_model,
                       "action": "edit" if images else "generate", "size": size,
                       "quality": quality, "output_format": output_format}],
            "tool_choice": {"type": "image_generation"}}


def redact(value, secrets):
    text = str(value)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[^\s\"\\]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\b(?:sk-|eyJ)[A-Za-z0-9_.-]+", "[REDACTED]", text)
    return text[:1500]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_dns_hostname(host):
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if "." not in host or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".arpa", ".test", ".invalid", ".example", ".onion")):
        return None
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def dns_addresses(data, hostname, rrtype):
    if not isinstance(data, dict) or data.get("Status") != 0:
        return []
    answers = data.get("Answer", [])
    if not isinstance(answers, list) or len(answers) > 128:
        return []
    names = {hostname.rstrip(".").lower()}
    for _ in range(8):
        for answer in answers:
            if isinstance(answer, dict) and answer.get("type") == 5 and str(answer.get("name", "")).rstrip(".").lower() in names:
                names.add(str(answer.get("data", "")).rstrip(".").lower())
    result = []
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != rrtype or str(answer.get("name", "")).rstrip(".").lower() not in names:
            continue
        if not isinstance(answer.get("data"), str):
            continue
        try:
            address = ipaddress.ip_address(answer.get("data", ""))
        except ValueError:
            continue
        if address.version == (4 if rrtype == 1 else 6) and address.is_global and not address.is_multicast:
            value = str(address)
            if value not in result:
                result.append(value)
    return result


def query_dns_resolver(source, hostname, deadline):
    server, path, bootstraps = DNS_RESOLVERS[source]
    addresses = []
    for rrtype in (1, 28):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = http.client.HTTPSConnection(server, timeout=min(2, remaining))

        def bootstrap(address, timeout, source_address=None):
            for ip in bootstraps:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    sock = socket.create_connection((ip, 443), min(1, remaining), source_address)
                    sock.settimeout(min(2, max(0.01, deadline - time.monotonic())))
                    return sock
                except OSError:
                    continue
            raise TimeoutError("DNS resolver unavailable.")

        # Connect via public bootstrap IP, retaining resolver TLS SNI and validation.
        connection._create_connection = bootstrap
        try:
            query = urllib.parse.urlencode({"name": hostname, "type": rrtype})
            connection.request("GET", path + "?" + query, headers={"Accept": "application/dns-json"})
            response = connection.getresponse()
            if response.status != 200:
                continue
            chunks, size = [], 0
            while time.monotonic() < deadline:
                chunk = response.read1(8192)
                if not chunk:
                    break
                size += len(chunk)
                if size > 65536:
                    raise ValueError("Oversized DNS response.")
                chunks.append(chunk)
            else:
                raise TimeoutError("DNS deadline exceeded.")
            addresses.extend(dns_addresses(json.loads(b"".join(chunks)), hostname, rrtype))
        except (OSError, ValueError, http.client.HTTPException):
            continue
        finally:
            connection.close()
    return addresses


def fallback_dns(hostname, mode="auto", budget=DNS_BUDGET):
    hostname = public_dns_hostname(hostname)
    if mode == "system" or not hostname:
        return [], None
    sources = ("cn", "global") if mode == "auto" else (mode,)
    deadline = time.monotonic() + budget
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(sources))
    pending = {pool.submit(query_dns_resolver, source, hostname, deadline): source for source in sources}
    try:
        for future in concurrent.futures.as_completed(pending, timeout=budget):
            try:
                addresses = future.result()
            except (OSError, ValueError, http.client.HTTPException):
                continue
            if addresses:
                return addresses, pending[future]
    except TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [], None


class ProviderHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, provider_host, dns_mode):
        super().__init__()
        self.provider_host = provider_host.lower()
        self.dns_mode = dns_mode

    def https_open(self, request):
        def connection(host, **kwargs):
            client = http.client.HTTPSConnection(host, **kwargs)
            original = client._create_connection

            def connect(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                try:
                    return original(address, timeout, source_address)
                except socket.gaierror as failure:
                    # HTTPS proxies resolve the target themselves; preserve their routing.
                    if client._tunnel_host or address[0].lower() != self.provider_host:
                        raise
                    addresses, source = fallback_dns(address[0], self.dns_mode)
                    deadline = time.monotonic() + DNS_BUDGET
                    for ip in addresses[:4]:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            sock = socket.create_connection((ip, address[1]), min(1, remaining), source_address)
                            sock.settimeout(timeout)
                            print("System DNS unavailable; encrypted DNS fallback connected (" + source + ").", file=sys.stderr, flush=True)
                            return sock
                        except OSError:
                            continue
                    raise failure from None

            client._create_connection = connect
            return client

        return self.do_open(connection, request, context=self._context)


def api_error(error, secrets):
    try:
        obj = error if isinstance(error, dict) else json.loads(error)
        failure = obj.get("error", obj)
        if not isinstance(failure, dict):
            return "API rejected the request."
        return redact({k: failure[k] for k in ["type", "code", "param", "message"] if k in failure}, secrets)
    except (ValueError, TypeError):
        return "API did not return a structured error."


def prepare_output(path, output_format):
    path = Path(path).expanduser().resolve()
    extensions = {"png": {".png"}, "jpeg": {".jpg", ".jpeg"}, "webp": {".webp"}}
    if path.suffix.lower() not in extensions[output_format]:
        raise SkillError("Output filename extension must match --output-format.")
    if path.exists():
        raise SkillError("Output already exists; choose a new filename before making another API request.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, scratch = tempfile.mkstemp(prefix=".imagegen-write-check-", dir=path.parent)
        os.close(fd)
        Path(scratch).unlink()
    except OSError:
        raise SkillError("Output directory is not writable; no request was sent.") from None
    return path


def save_image(data, path, output_format):
    info = image_info(data)
    if info["format"] != output_format:
        path = path.with_suffix(".jpg" if info["format"] == "jpeg" else "." + info["format"])
    try:
        file = path.open("xb")
    except FileExistsError:
        fd, filename = tempfile.mkstemp(prefix=path.stem + "-recovered-", suffix=path.suffix, dir=path.parent)
        file = os.fdopen(fd, "wb")
        path = Path(filename)
    with file:
        file.write(data)
    return {"path": str(path), **info}


def response_events(response, timeout):
    total, deadline = 0, time.monotonic() + timeout
    data = []
    event_size = 0
    while True:
        if time.monotonic() > deadline:
            raise SkillError("Response deadline exceeded; result is uncertain. Do not automatically resubmit.")
        line = response.readline(MAX_EVENT + 1)
        if not line:
            if data:
                yield json.loads("\n".join(data))
            return
        total += len(line)
        if total > MAX_RESPONSE or len(line) > MAX_EVENT:
            raise SkillError("Response exceeds safety limits; do not automatically resubmit.")
        line = line.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            value = line[5:].lstrip(" ")
            if value == "[DONE]":
                return
            event_size += len(line)
            if event_size > MAX_EVENT:
                raise SkillError("Image event exceeds safety limits; do not automatically resubmit.")
            data.append(value)
        elif not line and data:
            yield json.loads("\n".join(data))
            data, event_size = [], 0


def generate(connection, payload, out, timeout=240):
    images, seen = [], set()
    completed = False
    response_id = None
    response_received = False

    def consume(item):
        if not isinstance(item, dict) or item.get("type") != "image_generation_call" or not item.get("result"):
            return
        if item.get("status") in {"failed", "cancelled"}:
            raise SkillError("Image tool failed; do not automatically resubmit.")
        result = item["result"]
        if not isinstance(result, str) or len(result) > (MAX_IMAGE + 2) // 3 * 4:
            raise SkillError("Invalid or oversized image data; do not automatically resubmit.")
        digest = hashlib.sha256(result.encode("ascii")).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        data = base64.b64decode(result, validate=True)
        image_path = out if not images else out.with_name(out.stem + f"-{len(images) + 1}" + out.suffix)
        image = save_image(data, image_path, payload["tools"][0]["output_format"])
        images.append(image)
        print(json.dumps({"saved_image": image}, ensure_ascii=False), flush=True)

    def event(data):
        nonlocal completed, response_id
        kind = data.get("type")
        if kind == "response.output_item.done":
            consume(data.get("item"))
        elif kind in {"response.created", "response.completed", "response.failed", "response.incomplete"}:
            response = data.get("response", {})
            response_id = response.get("id") or response_id
            if kind in {"response.failed", "response.incomplete"}:
                raise SkillError("Response did not complete: " + api_error(response.get("error") or {}, connection["secrets"]))
            if kind == "response.completed":
                if response.get("status") not in {None, "completed"}:
                    raise SkillError("Response has a non-completed status; do not automatically resubmit.")
                for item in response.get("output", []):
                    consume(item)
                completed = True
        elif kind == "error":
            raise SkillError("API event error: " + api_error(data, connection["secrets"]))

    request = urllib.request.Request(connection["endpoint"], data=json.dumps(payload).encode("utf-8"),
                                     headers=connection["headers"], method="POST")
    try:
        handlers = [NoRedirect()]
        dns_mode = connection.get("dns_mode", "auto")
        if dns_mode != "system":
            handlers.append(ProviderHTTPSHandler(urllib.parse.urlsplit(connection["endpoint"]).hostname, dns_mode))
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
            response_received = True
            if "text/event-stream" in response.headers.get("Content-Type", ""):
                for data in response_events(response, timeout):
                    event(data)
            else:
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise SkillError("Response exceeds safety limits; do not automatically resubmit.")
                data = json.loads(raw)
                event({"type": "response.completed", "response": data})
    except urllib.error.HTTPError as error:
        if 300 <= error.code < 400:
            raise SkillError("API redirect refused; credentials were not forwarded to the redirect target.") from None
        detail = api_error(error.read(16384), connection["secrets"])
        suffix = " Result is uncertain; do not automatically resubmit." if error.code >= 500 else " No automatic retry was made."
        raise SkillError(f"API HTTP {error.code}: {detail}.{suffix}") from None
    except urllib.error.URLError as error:
        reason = error.reason
        if not response_received:
            if isinstance(reason, socket.gaierror):
                raise SkillError("DNS lookup failed before an API connection was established. No image-generation request was sent. Check DNS/provider configuration; if network access is restricted, request the required network permission before trying again.") from None
            if isinstance(reason, PermissionError):
                raise SkillError("Network connection was blocked by local permissions before an API connection was established. No image-generation request was sent. Request the required network permission before trying again.") from None
            if isinstance(reason, ConnectionRefusedError):
                raise SkillError("API connection was refused. No image-generation request was sent. Check provider availability and network access before trying again.") from None
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise SkillError("TLS certificate verification failed before the image-generation request was sent. Check the provider certificate or local trust configuration; do not disable certificate verification.") from None
        raise SkillError("API transport failed. Result may be uncertain; do not automatically resubmit. Preserve any saved images.") from None
    except TimeoutError:
        raise SkillError("API request timed out. Result may be uncertain; do not automatically resubmit. Preserve any saved images.") from None
    except OSError:
        raise SkillError("Response transport or file I/O failed. Result may be uncertain; do not automatically resubmit. Preserve any saved images.") from None
    except (ValueError, UnicodeError, binascii.Error):
        raise SkillError("API returned malformed image/event data. Do not automatically resubmit. Preserve any saved images.") from None
    if not completed:
        raise SkillError("Stream ended without response.completed. Result is uncertain; do not automatically resubmit. Preserve any saved images.")
    if not images:
        raise SkillError("API returned no image file. Text alone is not success. Check image-tool access and upstream account routing.")
    return {"status": "completed", "response_id": response_id, "model": payload["model"],
            "image_model_requested": payload["tools"][0]["model"],
            "size_requested": payload["tools"][0]["size"], "images": images}


def main(argv=None):
    # Pipes on Windows otherwise use a legacy code page for Chinese filenames.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "generate", "edit"])
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--image", action="append", default=[], type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--image-model", default=DEFAULT_IMAGE_MODEL)
    parser.add_argument("--size", default="auto")
    parser.add_argument("--quality", choices=["auto", "low", "medium", "high"], default="medium")
    parser.add_argument("--output-format", choices=["png", "jpeg", "webp"], default="png")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--dns", choices=["auto", "system", "cn", "global"], default="auto",
                        help="After system DNS fails: race cn/global (auto), use one source, or disable fallback (system)")
    args = parser.parse_args(argv)
    try:
        connection = resolve_connection(args.codex_home, args.profile)
        connection["dns_mode"] = args.dns
        if args.command == "inspect":
            print(json.dumps({k: connection[k] for k in ["provider", "profile", "endpoint", "key_source"]}, ensure_ascii=False))
            return 0
        if bool(args.prompt) == bool(args.prompt_file):
            raise SkillError("Specify exactly one of --prompt and --prompt-file.")
        if not args.out:
            raise SkillError("Specify --out with a new image filename.")
        if args.command == "edit" and not args.image:
            raise SkillError("Editing requires at least one --image reference.")
        if not 1 <= args.timeout <= 900:
            raise SkillError("Timeout must be between 1 and 900 seconds.")
        try:
            prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8-sig")
        except OSError:
            raise SkillError("Cannot read the prompt file.") from None
        payload = make_payload(prompt, args.image, args.model, args.image_model, args.size, args.quality, args.output_format)
        out = prepare_output(args.out, args.output_format)
        print(json.dumps(generate(connection, payload, out, args.timeout), ensure_ascii=False))
        return 0
    except SkillError as error:
        print("Image generation failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
