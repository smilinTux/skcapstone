"""Exercise the real per-task HTTP boundary against a synthetic gateway."""

import http.client
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_gateway_bridge as bridge


@contextmanager
def two_hop_gateway(reply):
    """Real TCP client -> private HTTP -> Unix bridge -> synthetic gateway."""
    forwarded, claims, waits = [], [], []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            forwarded.append(json.loads(raw))
            reply(self)

    upstream = HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gateway.sock"
            with bridge.gateway_socket(
                path, endpoint=f"http://127.0.0.1:{upstream.server_port}/v1",
                credential="SYNTHETIC-SECRET", family="deepseek", model="deepseek-flash",
                check_claim=lambda: claims.append(True),
            ) as host:
                def connect(connection):
                    waits.append(connection.timeout)
                    connection.sock = socket.socket(socket.AF_UNIX)
                    connection.sock.settimeout(connection.timeout)
                    connection.sock.connect(str(path))

                with patch.object(bridge.UnixConnection, "connect", connect):
                    private = HTTPServer(("127.0.0.1", 0), bridge.PrivateBridge)
                    worker = threading.Thread(target=private.serve_forever, daemon=True)
                    worker.start()
                    try:
                        def request(**options):
                            body = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "OK"}]}
                            body.update(options)
                            connection = http.client.HTTPConnection("127.0.0.1", private.server_port, timeout=5)
                            connection.request("POST", "/v1/chat/completions", json.dumps(body))
                            response = connection.getresponse()
                            result = response.status, dict(response.headers), response.read()
                            connection.close()
                            return result
                        yield request, host, forwarded, claims, waits
                    finally:
                        private.shutdown()
                        private.server_close()
                        worker.join(timeout=2)
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)


def successful_reply(handler, *, family="deepseek"):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("x-sk-provider", family)
    handler.send_header("x-sk-model-requested", "deepseek-flash")
    handler.send_header("x-sk-model-served", "deepseek-flash")
    handler.end_headers()
    handler.wfile.write(b'{"usage":{"total_tokens":1},"choices":[]}')


class BridgeTest(unittest.TestCase):
    def test_gateway_backpressure_survives_both_hops_without_secrets_or_retries(self):
        responses = iter([
            (429, "17", "17"), (503, "Wed, 21 Oct 2026 07:28:00 GMT", "Wed, 21 Oct 2026 07:28:00 GMT"),
            (402, "0", "0"), (429, "0000000000001", "1"),
            (429, "secret-token", None), (503, "-1", None),
            (429, "9" * 100, None), (401, None, None), (302, "1", None),
        ])
        current = []
        def reply(handler):
            status, delay, expected = current[-1]
            handler.send_response(status)
            if delay is not None:
                handler.send_header("Retry-After", delay)
            handler.send_header("X-Secret", "SYNTHETIC-SECRET")
            handler.send_header("Location", "http://secret.example/SYNTHETIC-SECRET")
            handler.end_headers()
            handler.wfile.write(b'SYNTHETIC-SECRET provider error body')
        with two_hop_gateway(reply) as (request, host, forwarded, claims, waits):
            for spec in responses:
                current.append(spec)
                status, headers, body = request(max_tokens=32)
                self.assertEqual(status, spec[0] if spec[0] >= 400 else 502)
                self.assertEqual(headers.get("Retry-After"), spec[2])
                self.assertNotIn("X-Secret", headers)
                self.assertNotIn("Location", headers)
                self.assertNotIn(b"SYNTHETIC-SECRET", body)
                self.assertEqual(len(forwarded), len(current))
            self.assertEqual(len(claims), len(current))
            self.assertNotIn("SYNTHETIC-SECRET", json.dumps(host.failures))

    def test_gateway_owns_output_budget_including_omitted_default(self):
        with two_hop_gateway(successful_reply) as (request, host, forwarded, claims, waits):
            for options in ({"max_tokens": 16384}, {"max_completion_tokens": 32768}, {}):
                self.assertEqual(request(**options)[0], 200)
                for key, value in options.items():
                    self.assertEqual(forwarded[-1][key], value)
            for value in (0, -1, True, "16384"):
                self.assertEqual(request(max_tokens=value)[0], 502)
            self.assertEqual(len(forwarded), 3)
            self.assertEqual(host.usage.totals, {"total_tokens": 3})

    def test_gateway_queue_wait_has_a_longer_timeout_chain_and_no_replay(self):
        def delayed(handler):
            time.sleep(0.2)
            successful_reply(handler)
        with two_hop_gateway(delayed) as (request, host, forwarded, claims, waits):
            actual_open = host.opener.open
            gateway_waits = []
            def scaled_open(req, timeout):
                gateway_waits.append(timeout)
                return actual_open(req, timeout=timeout / 1000)
            host.opener.open = scaled_open
            self.assertEqual(request(max_tokens=32)[0], 200)
            # Scale wall time only: old120ms would fail the real200ms wait.
            self.assertGreaterEqual(gateway_waits[0], 300 + 300 + 30)
            self.assertGreater(waits[0], gateway_waits[0])
            self.assertEqual(len(forwarded), 1)

    def test_attribution_failure_never_releases_provider_content(self):
        with two_hop_gateway(lambda h: successful_reply(h, family="wrong")) as (request, host, forwarded, claims, waits):
            status, headers, body = request(max_tokens=32)
            self.assertEqual(status, 502)
            self.assertNotIn(b"usage", body)
            self.assertEqual(host.usage.with_usage, 0)
            self.assertEqual(len(forwarded), 1)

    def test_uncertain_timeout_is_not_replayed(self):
        def delayed(handler):
            time.sleep(0.1)
            try:
                successful_reply(handler)
            except BrokenPipeError:
                pass
        with two_hop_gateway(delayed) as (request, host, forwarded, claims, waits):
            actual_open = host.opener.open
            host.opener.open = lambda req, timeout: actual_open(req, timeout=0.02)
            self.assertEqual(request(max_tokens=32)[0], 502)
            time.sleep(0.15)
            self.assertEqual(len(forwarded), 1)
            self.assertEqual(len(host.failures), 1)
            self.assertEqual(host.usage.with_usage, 0)

    def test_retry_timing_rejects_header_injection_and_invalid_dates(self):
        for value in (None, "", "10\r\nX-Secret: injected", "\u0661\u0662", "tomorrow", "Wed, 99 Oct 2026 07:28:00 GMT"):
            with self.subTest(value=value):
                self.assertIsNone(bridge.retry_after(value))

    def test_existing_or_symlinked_usage_receipt_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "gateway.sock"
            receipt = Path(td) / "usage.json"
            for symlink in (False, True):
                if symlink:
                    receipt.symlink_to(Path(td) / "missing-target")
                else:
                    receipt.write_text("original")
                with self.assertRaisesRegex(ValueError, "usage-receipt-exists"):
                    with bridge.gateway_socket(
                        path,
                        endpoint="http://unused/v1",
                        credential="synthetic",
                        family="zai",
                        model="sk-zai-m",
                        check_claim=lambda: None,
                    ):
                        self.fail("receipt collision admitted")
                self.assertFalse(path.exists())
                if not symlink:
                    self.assertEqual(receipt.read_text(), "original")
                receipt.unlink()

    def test_missing_fields_are_not_reported_as_zero_and_usage_is_bounded(self):
        usage = bridge.Usage()
        usage.observe(b'{"usage":{"completion_tokens":0}}', "application/json")
        usage.observe(
            b'{"usage":{"prompt_tokens":9223372036854775808,'
            b'"completion_tokens_details":{"reasoning_tokens":4}}}',
            "application/json",
        )
        usage.observe(b"[" * 2000 + b"]" * 2000, "application/json")
        self.assertEqual(usage.totals, {"completion_tokens": 0, "reasoning_tokens": 4})
        self.assertEqual(usage.with_usage, 2)
        self.assertEqual(usage.responses, 3)
        self.assertEqual(usage.invalid, 2)

    def test_usage_uses_final_stream_observation_and_rejects_invalid_numbers(self):
        usage = bridge.Usage()
        payload = b'data: {"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n'
        payload += (
            b'data: {"usage":{"prompt_tokens":2,"completion_tokens":3,'
            b'"total_tokens":5,"prompt_tokens_details":{"cached_tokens":1}}}\n\n'
        )
        payload += b"data: [DONE]\n\n"
        usage.observe(payload, "text/event-stream; charset=utf-8")
        usage.observe(
            b'{"usage":{"prompt_tokens":true,"completion_tokens":-1,"total_tokens":1.5}}',
            "application/json",
        )
        usage.observe(b'{"choices":[{"message":{"content":"PRIVATE"}}]}', "application/json")
        usage.observe(b"not JSON", "application/json")
        self.assertEqual(
            usage.totals,
            {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cached_prompt_tokens": 1,
            },
        )
        self.assertEqual(usage.coverage, {key: 1 for key in usage.totals})
        self.assertEqual(usage.responses, 4)
        self.assertEqual(usage.with_usage, 1)
        self.assertEqual(usage.invalid, 2)

    def test_fragmented_stream_bytes_are_unchanged_and_usage_is_observed(self):
        payload = b'data: {"choices":[{"delta":{"content":"PRIVATE"}}]}\n\n'
        payload += (
            b'data: {"usage":{"prompt_tokens":11,"completion_tokens":4,"total_tokens":15}}\n\n'
        )
        payload += b"data: [DONE]\n\n"

        class Response:
            status = 200
            headers = {"Content-Type": "text/event-stream"}

            def __init__(self):
                self.chunks = iter([payload[i : i + 7] for i in range(0, len(payload), 7)])

            def read1(self, size):
                return next(self.chunks, b"")

        handler = object.__new__(bridge.GatewayHandler)
        handler.server = type("Server", (), {"usage": bridge.Usage()})()
        handler.wfile = io.BytesIO()
        handler.send_response = lambda value: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.copy_response(Response())
        self.assertEqual(handler.wfile.getvalue(), payload)
        self.assertEqual(handler.server.usage.totals["total_tokens"], 15)

    def test_real_bridge_scopes_auth_identity_claim_without_request_budget(self):
        forwarded = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                forwarded.append((self.path, self.headers.get("Authorization"), body))
                self.send_response(200)
                for k, v in {
                    "Content-Type": "application/json",
                    "x-sk-provider": "zai",
                    "x-sk-model-requested": "sk-zai-m",
                    "x-sk-bucket-member": "glm-5.3",
                    "x-sk-model-served": "glm-5.3",
                    "X-Secret": "do-not-forward",
                }.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(
                    b'{"model":"glm-5.3","choices":[{"message":'
                    b'{"content":"PRIVATE-RESPONSE"}}],"usage":'
                    b'{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}'
                )

        upstream = HTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "gateway.sock"
                claims = []
                with bridge.gateway_socket(
                    path,
                    endpoint="http://127.0.0.1:" + str(upstream.server_port) + "/v1",
                    credential="synthetic-secret",
                    family="zai",
                    model="sk-zai-m",
                    check_claim=lambda: claims.append("checked"),
                ) as server:

                    def request(route="/v1/chat/completions", model="sk-zai-m"):
                        connection = http.client.HTTPConnection("unused", timeout=5)
                        connection.sock = socket.socket(socket.AF_UNIX)
                        connection.sock.settimeout(5)
                        connection.sock.connect(str(path))
                        body = json.dumps(
                            {
                                "model": model,
                                "messages": [{"role": "user", "content": "OK"}],
                                "max_tokens": 32,
                            }
                        )
                        connection.request(
                            "POST", route, body, {"Authorization": "attacker-selected"}
                        )
                        response = connection.getresponse()
                        answer = response.read()
                        result = response.status, dict(response.headers), answer
                        connection.close()
                        return result

                    status, headers, payload = request()
                    self.assertEqual(status, 200)
                    self.assertNotIn("X-Secret", headers)
                    self.assertNotIn(b"synthetic-secret", payload)
                    self.assertEqual(forwarded[0][0], "/v1/chat/completions")
                    self.assertEqual(forwarded[0][1], "Bearer synthetic-secret")
                    self.assertEqual(claims, ["checked"])
                    self.assertEqual(request("/admin")[0], 502)
                    self.assertEqual(request(model="other")[0], 502)
                    self.assertEqual(len(forwarded), 1)
                    server.check_claim = lambda: (_ for _ in ()).throw(ValueError("revoked"))
                    self.assertEqual(request()[0], 502)
                    self.assertEqual(len(forwarded), 1)
                    server.check_claim = lambda: None
                    for _ in range(65):
                        self.assertEqual(request()[0], 200)
                    self.assertEqual(len(forwarded), 66)
                    with patch.object(bridge, "MAX_RESPONSE", 8):
                        status, headers, payload = request()
                    self.assertEqual(status, 200)
                    self.assertEqual(payload, b"")
                    self.assertNotIn(b"HTTP/1", payload)
                receipt_path = path.with_name("usage.json")
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(receipt["local_budget_refusals"], 0)
                self.assertEqual(receipt["usage_totals"]["total_tokens"], 66 * 9)
                self.assertEqual(receipt["responses_with_usage"], 66)
                self.assertFalse(receipt["accepted"])
                for forbidden in (
                    "synthetic-secret",
                    "PRIVATE-RESPONSE",
                    "attacker-selected",
                    "do-not-forward",
                ):
                    self.assertNotIn(forbidden, receipt_path.read_text())
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
