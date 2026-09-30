"""Exercise the real per-task HTTP boundary against a synthetic gateway."""
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_gateway_bridge as bridge


class BridgeTest(unittest.TestCase):
    def test_real_bridge_scopes_auth_identity_claim_and_request_budget(self):
        forwarded = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                forwarded.append((self.path, self.headers.get('Authorization'), body))
                self.send_response(200)
                for k, v in {'Content-Type': 'application/json', 'x-sk-provider': 'zai',
                             'x-sk-model-requested': 'sk-zai-m', 'x-sk-bucket-member': 'glm-5.3',
                             'x-sk-model-served': 'glm-5.3', 'X-Secret': 'do-not-forward'}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(b'{"model":"glm-5.3","choices":[{"message":{"content":"OK"}}]}')
        upstream = HTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / 'gateway.sock'
                claims = []
                with bridge.gateway_socket(path, endpoint='http://127.0.0.1:' + str(upstream.server_port) + '/v1',
                        credential='synthetic-secret', family='zai', model='sk-zai-m',
                        check_claim=lambda: claims.append('checked')) as server:
                    def request(route='/v1/chat/completions', model='sk-zai-m'):
                        connection = http.client.HTTPConnection('unused', timeout=5)
                        connection.sock = socket.socket(socket.AF_UNIX)
                        connection.sock.settimeout(5)
                        connection.sock.connect(str(path))
                        body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': 'OK'}],
                                           'max_tokens': 32})
                        connection.request('POST', route, body, {'Authorization': 'attacker-selected'})
                        response = connection.getresponse()
                        answer = response.read()
                        result = response.status, dict(response.headers), answer
                        connection.close()
                        return result
                    status, headers, payload = request()
                    self.assertEqual(status, 200)
                    self.assertNotIn('X-Secret', headers)
                    self.assertNotIn(b'synthetic-secret', payload)
                    self.assertEqual(forwarded[0][0], '/v1/chat/completions')
                    self.assertEqual(forwarded[0][1], 'Bearer synthetic-secret')
                    self.assertEqual(claims, ['checked'])
                    self.assertEqual(request('/admin')[0], 502)
                    self.assertEqual(request(model='other')[0], 502)
                    self.assertEqual(len(forwarded), 1)
                    server.check_claim = lambda: (_ for _ in ()).throw(ValueError('revoked'))
                    self.assertEqual(request()[0], 502)
                    self.assertEqual(len(forwarded), 1)
                    server.requests = bridge.MAX_REQUESTS
                    self.assertEqual(request()[0], 429)
                    server.requests = 0
                    server.check_claim = lambda: None
                    with patch.object(bridge, 'MAX_RESPONSE', 8):
                        status, headers, payload = request()
                    self.assertEqual(status, 200)
                    self.assertEqual(payload, b'')
                    self.assertNotIn(b'HTTP/1', payload)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
