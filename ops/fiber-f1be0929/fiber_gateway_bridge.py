"""Per-task completion-only bridge. Credentials never enter the sandbox."""

import http.client
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.request
from contextlib import contextmanager
from datetime import timezone
from email.utils import format_datetime, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MAX_BODY = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
# Evidence memory is bounded independently of the number of model requests.
MAX_FAILURES = 64
# Gateway may queue 300s, execute 300s, and wait 30s for a provider retry.
GATEWAY_TIMEOUT = 660
PRIVATE_TIMEOUT = GATEWAY_TIMEOUT + 30


class Usage:
    """Aggregate only provider-reported counts, with explicit field coverage."""

    def __init__(self):
        self.totals, self.coverage = {}, {}
        self.responses, self.with_usage, self.invalid = 0, 0, 0

    def observe(self, raw, content_type):
        self.responses += 1
        usage, invalid = None, False
        frames = (
            (line[5:].strip() for line in raw.splitlines() if line.startswith(b"data:"))
            if content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
            else (raw,)
        )
        for frame in frames:
            if not frame or frame == b"[DONE]":
                continue
            try:
                value = json.loads(frame)
            except (ValueError, UnicodeError, RecursionError):
                invalid = True
                continue
            if not isinstance(value, dict):
                invalid = True
                continue
            if value.get("usage") is not None:
                usage = value["usage"]  # Streaming usage is cumulative, not additive.
        counts = {}
        if usage is not None:
            if not isinstance(usage, dict):
                invalid = True
            else:
                fields = {
                    key: usage.get(key)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
                for parent, key, target in (
                    ("prompt_tokens_details", "cached_tokens", "cached_prompt_tokens"),
                    ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
                ):
                    details = usage.get(parent)
                    if isinstance(details, dict):
                        fields[target] = details.get(key)
                    elif details is not None:
                        invalid = True
                for key, value in fields.items():
                    if value is None:
                        continue
                    if type(value) is int and 0 <= value <= 2**63 - 1:
                        counts[key] = value
                    else:
                        invalid = True
        for key, value in counts.items():
            self.totals[key] = self.totals.get(key, 0) + value
            self.coverage[key] = self.coverage.get(key, 0) + 1
        self.with_usage += bool(counts)
        self.invalid += invalid


def validate_payload(path, raw, model):
    """Accept only bounded completion bodies for the exact assigned model."""
    if path != "/v1/chat/completions" or len(raw) > MAX_BODY:
        raise ValueError("completion-route-refused")
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("model") != model:
        raise ValueError("completion-model-refused")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("completion-messages-refused")
    allowed = {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "stop",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning_effort",
        "thinking",
        "frequency_penalty",
        "presence_penalty",
        "seed",
        "n",
        "store",
        "prompt_cache_key",
        "prompt_cache_retention",
    }
    if set(body) - allowed or body.get("n", 1) != 1 or body.get("store", False) is not False:
        raise ValueError("completion-options-refused")
    for field in ("max_tokens", "max_completion_tokens"):
        if field in body and (type(body[field]) is not int or body[field] < 1):
            raise ValueError("completion-budget-refused")
    return body


def retry_after(value):
    """Relay only normalized HTTP retry timing, never arbitrary header text."""
    if not isinstance(value, str) or len(value) > 64 or not value.isascii():
        return None
    if value.isdecimal():
        return str(int(value))
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        date = parsedate_to_datetime(value)
        return format_datetime(date.astimezone(timezone.utc), usegmt=True) if date.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Never forward a gateway credential to another endpoint."""
        return None


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        """Avoid logging prompts, headers or provider responses."""

    def setup(self):
        super().setup()
        self.connection.settimeout(PRIVATE_TIMEOUT)
        self.response_started = False

    def read_body(self):
        """Refuse ambiguous framing and oversized requests before allocation."""
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            raise ValueError("request-framing")
        size = int(lengths[0])
        if not 0 < size <= MAX_BODY:
            raise ValueError("request-size")
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError("request-truncated")
        return raw

    def copy_response(self, response):
        """Stream bounded bytes without propagating upstream secret headers."""
        self.send_response(response.status)
        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
        delay = retry_after(response.headers.get("Retry-After"))
        if response.status in {402, 429, 503} and delay is not None:
            self.send_header("Retry-After", delay)
        self.send_header("Connection", "close")
        self.end_headers()
        self.response_started = True
        total = 0
        usage = getattr(self.server, "usage", None)
        recorded = bytearray() if usage is not None else None
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise ValueError("response-size")
            self.wfile.write(chunk)
            self.wfile.flush()
            if recorded is not None:
                recorded.extend(chunk)
        self.close_connection = True
        if recorded is not None:
            # Bounded by MAX_RESPONSE; only aggregates survive this request.
            usage.observe(recorded, response.headers.get("Content-Type", "application/json"))


class GatewayHandler(QuietHandler):
    def do_POST(self):
        """Use the controller's pinned endpoint, credential and claim check."""
        # Only this host-side handler needs route metadata. The same script is
        # mounted alone inside the sandbox for its credential-free private hop.
        from fiber_routes import provider_matches

        try:
            raw = self.read_body()
            self.server.requests += 1
            validate_payload(self.path, raw, self.server.model)
            self.server.check_claim()
            request = urllib.request.Request(
                self.server.endpoint + "/chat/completions",
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.server.credential,
                },
            )
            with self.server.opener.open(request, timeout=GATEWAY_TIMEOUT) as response:
                requested = response.headers.get("x-sk-model-requested")
                served = response.headers.get("x-sk-model-served")
                selected = (
                    response.headers.get("x-sk-bucket-member")
                    if self.server.model.startswith("sk-")
                    else self.server.model
                )
                if (
                    not provider_matches(self.server.family, self.server.model, response.headers)
                    or requested != self.server.model
                    or not selected
                    or served != selected
                ):
                    raise ValueError("gateway-attribution-refused")
                self.copy_response(response)
        except Exception as exc:
            failure = {"type": type(exc).__name__}
            if isinstance(exc, urllib.error.HTTPError):
                failure["http_status"] = exc.code
            if isinstance(exc, ValueError) and str(exc) in {
                "gateway-attribution-refused",
                "completion-model-refused",
                "response-size",
            }:
                failure["reason"] = str(exc)
            if len(self.server.failures) < MAX_FAILURES:
                self.server.failures.append(failure)
            # No raw exception text: it may include tokens, URLs or prompt text.
            self.close_connection = True
            if not self.response_started:
                try:
                    status = exc.code if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code <= 599 else 502
                    delay = retry_after(exc.headers.get("Retry-After")) if isinstance(exc, urllib.error.HTTPError) else None
                    payload = json.dumps({"error": {"type": "gateway_request_refused", "status": status}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Connection", "close")
                    if status in {402, 429, 503} and delay is not None:
                        self.send_header("Retry-After", delay)
                    self.end_headers()
                    self.response_started = True
                    self.wfile.write(payload)
                except OSError:
                    pass
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()


@contextmanager
def gateway_socket(path, *, endpoint, credential, family, model, check_claim):
    """Serve a single task serially on a private, non-reusable Unix socket."""
    usage_path = Path(path).with_name("usage.json")
    if usage_path.exists() or usage_path.is_symlink():
        raise ValueError("usage-receipt-exists")
    server = socketserver.UnixStreamServer(str(path), GatewayHandler)
    Path(path).chmod(0o600)
    server.endpoint, server.credential = endpoint, credential
    server.family, server.model = family, model
    server.check_claim, server.requests = check_claim, 0
    server.failures = []
    server.usage, server.budget_refusals = Usage(), 0
    server.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        receipt = {
            "schema": "skfleet.usage/v1",
            "provider": family,
            "requested_model": model,
            "requests": server.requests,
            "local_budget_refusals": server.budget_refusals,
            "responses_with_usage": server.usage.with_usage,
            "responses_without_usage": server.usage.responses - server.usage.with_usage,
            "responses_with_invalid_usage": server.usage.invalid,
            "usage_totals": server.usage.totals,
            "usage_observations": server.usage.coverage,
            "bridge_failures": server.failures,
            "accepted": False,
        }
        descriptor = os.open(
            usage_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        """Only connect to the one socket supplied by the trusted launcher."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/run/fleet/gateway.sock")


class PrivateBridge(QuietHandler):
    def do_POST(self):
        """Translate private-loopback HTTP to the fixed host-side socket."""
        connection = UnixConnection("unused", timeout=PRIVATE_TIMEOUT)
        try:
            raw = self.read_body()
            connection.request(
                "POST", self.path, body=raw, headers={"Content-Type": "application/json"}
            )
            self.copy_response(connection.getresponse())
        except Exception:
            self.close_connection = True
            if not self.response_started:
                try:
                    self.send_error(502, "private-bridge-failed")
                except OSError:
                    pass
        finally:
            connection.close()


def inside(argv):
    """Run only inside the private network namespace; stop with the model."""
    status = Path("/proc/self/status").read_text()
    interfaces = [
        line.split(":")[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:]
    ]
    mounts = [line.split() for line in Path("/proc/self/mountinfo").read_text().splitlines()]
    readonly = {row[4] for row in mounts if "ro" in row[5].split(",")}
    if (
        "NoNewPrivs:\t1" not in status
        or "CapEff:\t0000000000000000" not in status
        or interfaces != ["lo"]
        or not {"/", "/run/fleet"}.issubset(readonly)
    ):
        raise RuntimeError("sandbox-kernel-boundary-unqualified")
    server = HTTPServer(("127.0.0.1", 18790), PrivateBridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return subprocess.run(argv).returncode
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(inside(sys.argv[1:]))
