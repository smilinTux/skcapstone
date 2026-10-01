"""Gateway-owned generation defaults across real HTTP and SDK boundaries."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_worker as worker
import fiber_sandbox as sandbox
from fiber_admission import Refused
from test_fiber_worker import request


def catalog(endpoint):
    return {'providers': {'skgw-deepseek': {'baseUrl': endpoint,
        'api': 'openai-completions', 'apiKey': 'synthetic-secret',
        'models': [{'id': 'deepseek-flash', 'contextWindow': 131072,
                    'maxTokens': 16384, 'compat': {'supportsDeveloperRole': False}}]}}}


def policy(value=8192):
    return {'data': [{'id': 'deepseek-flash', 'card': {'generation_default_tokens': value}}]}


@contextmanager
def gateway(body, status=200, headers=None):
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class GenerationBudgetTest(unittest.TestCase):
    def fetch(self, endpoint):
        return worker.gateway_generation_budget(catalog(endpoint), 'skgw-deepseek',
                                                'deepseek-flash', 'synthetic-secret')

    def test_exact_gateway_policy_above_old_ceiling_and_safe_integer(self):
        for value in (8192, 16384, 2**53 - 1):
            with self.subTest(value=value), gateway(policy(value)) as (endpoint, seen), \
                 patch.object(worker, 'GATEWAY', endpoint), \
                 patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}):
                self.assertEqual(self.fetch(endpoint), value)
                self.assertEqual(seen, [('/v1/models', 'Bearer synthetic-secret')])

    def test_invalid_policy_refuses_without_fallback(self):
        invalid = [None, {}, {'data': None}, {'data': []}, {'data': [None]},
                   {'data': policy()['data'] * 2}, {'data': [{'id': 'other', 'card': policy()['data'][0]['card']}]},
                   {'data': [{'id': 'deepseek-flash'}]}, b'{invalid json']
        invalid += [policy(value) for value in (None, True, False, 0, -1, 1.5, '8192', 2**53, 10**100)]
        for body in invalid:
            with self.subTest(body=body), gateway(body) as (endpoint, _), patch.object(worker, 'GATEWAY', endpoint):
                with self.assertRaises(Refused) as error:
                    self.fetch(endpoint)
                self.assertNotIn('synthetic-secret', str(error.exception))

    def test_bounded_body_and_http_failure_hide_response(self):
        for body, status in [(b' ' * (1024 * 1024 + 1), 200), (b'synthetic-secret', 401), (b'synthetic-secret', 503)]:
            with self.subTest(status=status), gateway(body, status) as (endpoint, _), patch.object(worker, 'GATEWAY', endpoint):
                with self.assertRaises(Refused) as error:
                    self.fetch(endpoint)
                self.assertNotIn('synthetic-secret', str(error.exception))

    def test_no_redirect_and_mismatched_catalog_sends_nothing(self):
        with gateway(policy()) as (target, target_seen), \
             gateway(b'', 302, {'Location': target + '/models'}) as (endpoint, seen), \
             patch.object(worker, 'GATEWAY', endpoint):
            with self.assertRaises(Refused):
                self.fetch(endpoint)
            self.assertEqual(len(seen), 1)
            self.assertEqual(target_seen, [])
            with self.assertRaises(Refused):
                worker.gateway_generation_budget(catalog(target), 'skgw-deepseek',
                                                 'deepseek-flash', 'synthetic-secret')
            self.assertEqual(len(seen), 1)
            self.assertEqual(target_seen, [])

    def test_missing_policy_stops_host_before_any_model_startup(self):
        with tempfile.TemporaryDirectory() as td, gateway({'data': []}) as (endpoint, seen):
            home = Path(td)
            directory = home / '.pi/agent'
            directory.mkdir(parents=True)
            (directory / 'models.json').write_text(json.dumps(catalog(endpoint)))
            with patch.object(worker.Path, 'home', return_value=home), \
                 patch.object(worker.socket, 'gethostname', return_value='chiap02'), \
                 patch.object(worker, 'GATEWAY', endpoint), patch.object(worker, 'verify_claim', return_value={}), \
                 patch.object(worker, 'verify_resource_limits') as resources, \
                 patch.object(sandbox, 'probe') as startup, patch.object(sandbox, 'run_logged') as model, \
                 patch.object(worker.subprocess, 'run') as run:
                with self.assertRaisesRegex(Refused, 'gateway-generation'):
                    worker.execute(request())
                resources.assert_not_called()
                startup.assert_not_called()
                model.assert_not_called()
                run.assert_not_called()
            self.assertEqual(len(seen), 1)

    def test_public_catalog_preserves_capabilities_and_requires_policy(self):
        source = catalog('http://chiap01:18790/v1')
        result = sandbox.public_catalog(source, 'skgw-deepseek', 'deepseek-flash', 32768)
        model = result['providers']['skgw-deepseek']['models'][0]
        self.assertEqual(model['maxTokens'], 32768)
        self.assertEqual(model['contextWindow'], 131072)
        self.assertFalse(model['compat']['supportsDeveloperRole'])
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertEqual(source['providers']['skgw-deepseek']['models'][0]['maxTokens'], 16384)
        for value in (None, True, 0, -1, 1.5, 2**53):
            with self.subTest(value=value), self.assertRaises(Refused):
                sandbox.public_catalog(source, 'skgw-deepseek', 'deepseek-flash', value)

    def test_actual_sdk_wire_budget_and_explicit_caller_override(self):
        sdk = Path.home() / '.npm-global/lib/node_modules/@earendil-works/pi-coding-agent'
        if not (sdk / 'dist/core/provider-composer.js').is_file():
            self.skipTest('actual Pi SDK not installed on this test host')
        script = r'''
import http from 'node:http';
import { pathToFileURL } from 'node:url';
const sdk = process.argv[1];
const { composeModelProvider } = await import(pathToFileURL(sdk + '/dist/core/provider-composer.js'));
const { streamSimple } = await import(pathToFileURL(sdk + '/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js'));
let input=''; for await (const chunk of process.stdin) input += chunk;
const catalogs=JSON.parse(input), captured=[];
const server=http.createServer(async(req,res)=>{
  let raw=''; for await(const chunk of req) raw+=chunk;
  const body=JSON.parse(raw);
  captured.push({model:body.model,budget:body.max_completion_tokens ?? body.max_tokens});
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  for(const [delta,finish_reason] of [[{role:'assistant',content:'OK'},null],[{},'stop']])
    res.write('data: '+JSON.stringify({id:'synthetic',model:body.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');
  res.end('data: [DONE]\n\n');
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
try {
  for(const [catalog,options] of catalogs){
    const [provider,config]=Object.entries(catalog.providers)[0];
    config.baseUrl=`http://127.0.0.1:${server.address().port}/v1`;
    const model=composeModelProvider(provider,undefined,{getProvider:()=>config},undefined).getModels()[0];
    const result=await streamSimple(model,{messages:[{role:'user',content:'Synthetic',timestamp:1}]},
      {apiKey:'synthetic-only',maxRetries:0,...options}).result();
    if(result.stopReason!=='stop') throw new Error('synthetic SDK completion failed');
  }
  console.log(JSON.stringify(captured));
} finally { await new Promise(resolve=>server.close(resolve)); }
'''
        source = catalog('http://chiap01:18790/v1')
        inputs = [(sandbox.public_catalog(source, 'skgw-deepseek', 'deepseek-flash', value), options)
                  for value, options in [(16384, {}), (32768, {}),
                                         (32768, {'maxTokens': 10000}), (32768, {'maxTokens': 40000})]]
        result = subprocess.run([str(sandbox.node_executable()), '--input-type=module', '-e', script, str(sdk)],
                                input=json.dumps(inputs), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [{'model': 'deepseek-flash', 'budget': value}
                                                    for value in (16384, 32768, 10000, 40000)])


if __name__ == '__main__':
    unittest.main()
