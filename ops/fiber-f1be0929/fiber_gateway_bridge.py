"""Per-task completion-only bridge. Credentials never enter the sandbox."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.request

MAX_BODY = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
MAX_REQUESTS = 64


def validate_payload(path, raw, model):
    """Accept only bounded completion bodies for the exact assigned model."""
    if path != '/v1/chat/completions' or len(raw) > MAX_BODY:
        raise ValueError('completion-route-refused')
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get('model') != model:
        raise ValueError('completion-model-refused')
    if not isinstance(body.get('messages'), list) or not body['messages']:
        raise ValueError('completion-messages-refused')
    allowed = {'model', 'messages', 'max_tokens', 'max_completion_tokens', 'stream', 'stream_options',
               'temperature', 'top_p', 'stop', 'tools', 'tool_choice', 'parallel_tool_calls',
               'reasoning_effort', 'thinking', 'frequency_penalty', 'presence_penalty', 'seed', 'n',
               'store', 'prompt_cache_key', 'prompt_cache_retention'}
    if set(body) - allowed or body.get('n', 1) != 1 or body.get('store', False) is not False:
        raise ValueError('completion-options-refused')
    for field in ('max_tokens', 'max_completion_tokens'):
        if field in body and (type(body[field]) is not int or not 1 <= body[field] <= 8192):
            raise ValueError('completion-budget-refused')
    if not any(field in body for field in ('max_tokens', 'max_completion_tokens')):
        raise ValueError('completion-budget-required')
    return body


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Never forward a gateway credential to another endpoint."""
        return None


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        """Avoid logging prompts, headers or provider responses."""

    def setup(self):
        super().setup()
        self.connection.settimeout(150)
        self.response_started = False

    def read_body(self):
        """Refuse ambiguous framing and oversized requests before allocation."""
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or self.headers.get('Transfer-Encoding'):
            raise ValueError('request-framing')
        size = int(lengths[0])
        if not 0 < size <= MAX_BODY:
            raise ValueError('request-size')
        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError('request-truncated')
        return raw

    def copy_response(self, response):
        """Stream bounded bytes without propagating upstream secret headers."""
        self.send_response(response.status)
        self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.response_started = True
        total = 0
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise ValueError('response-size')
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True


class GatewayHandler(QuietHandler):
    def do_POST(self):
        """Use the controller's pinned endpoint, credential and claim check."""
        try:
            raw = self.read_body()
            if self.server.requests >= MAX_REQUESTS:
                self.send_error(429, 'task-request-budget-exhausted')
                return
            self.server.requests += 1
            validate_payload(self.path, raw, self.server.model)
            self.server.check_claim()
            request = urllib.request.Request(self.server.endpoint + '/chat/completions',
                data=raw, headers={'Content-Type': 'application/json',
                                   'Authorization': 'Bearer ' + self.server.credential})
            with self.server.opener.open(request, timeout=120) as response:
                family = response.headers.get('x-sk-provider', response.headers.get('x-sk-backend'))
                requested = response.headers.get('x-sk-model-requested')
                served = response.headers.get('x-sk-model-served')
                selected = response.headers.get('x-sk-bucket-member') if self.server.model.startswith('sk-') else self.server.model
                if family != self.server.family or requested != self.server.model or not selected or served != selected:
                    raise ValueError('gateway-attribution-refused')
                self.copy_response(response)
        except Exception:
            # No raw exception text: it may include tokens, URLs or prompt text.
            self.close_connection = True
            if not self.response_started:
                try:
                    self.send_error(502, 'bounded-gateway-request-refused')
                except OSError:
                    pass


@contextmanager
def gateway_socket(path, *, endpoint, credential, family, model, check_claim):
    """Serve a single task serially on a private, non-reusable Unix socket."""
    server = socketserver.UnixStreamServer(str(path), GatewayHandler)
    Path(path).chmod(0o600)
    server.endpoint, server.credential = endpoint, credential
    server.family, server.model = family, model
    server.check_claim, server.requests = check_claim, 0
    server.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        """Only connect to the one socket supplied by the trusted launcher."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/run/fleet/gateway.sock')


class PrivateBridge(QuietHandler):
    def do_POST(self):
        """Translate private-loopback HTTP to the fixed host-side socket."""
        connection = UnixConnection('unused', timeout=150)
        try:
            raw = self.read_body()
            connection.request('POST', self.path, body=raw, headers={'Content-Type': 'application/json'})
            self.copy_response(connection.getresponse())
        except Exception:
            self.close_connection = True
            if not self.response_started:
                try:
                    self.send_error(502, 'private-bridge-failed')
                except OSError:
                    pass
        finally:
            connection.close()


def inside(argv):
    """Run only inside the private network namespace; stop with the model."""
    status = Path('/proc/self/status').read_text()
    interfaces = [line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:]]
    mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    readonly = {row[4] for row in mounts if 'ro' in row[5].split(',')}
    if ('NoNewPrivs:\t1' not in status or 'CapEff:\t0000000000000000' not in status
            or interfaces != ['lo'] or not {'/', '/run/fleet'}.issubset(readonly)):
        raise RuntimeError('sandbox-kernel-boundary-unqualified')
    server = HTTPServer(('127.0.0.1', 18790), PrivateBridge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return subprocess.run(argv).returncode
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    sys.exit(inside(sys.argv[1:]))
