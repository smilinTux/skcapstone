"""Two bounded synthetic completions through the isolated, actual Pi SDK."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fiber_sandbox
from fiber_gateway_bridge import gateway_socket

SCRIPT = '''
import { readFileSync } from 'node:fs';
import { streamSimple } from '/opt/pi/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js';
const catalog = JSON.parse(readFileSync('/work/.fiber-runtime/home/.pi/agent/models.json'));
const [provider, definition] = Object.entries(catalog.providers)[0];
const model = { cost: {input:0, output:0, cacheRead:0, cacheWrite:0}, contextWindow:65536,
                ...definition.models[0], provider, baseUrl:definition.baseUrl };
let fields;
const answer = await streamSimple(model, {messages:[{role:'user',content:'Reply only OK.',timestamp:1}]},
  {apiKey:'isolated-controller', reasoning:'off', maxTokens:1024,
   onPayload(value) { fields = Object.keys(value); }}).result();
const text = answer.content.filter(x => x.type === 'text').map(x => x.text).join('');
console.log(JSON.stringify({provider, model:model.id, stopReason:answer.stopReason,
                           nonempty:!!text.trim(), request_fields:fields, error:answer.errorMessage}));
if (!text.trim() || answer.stopReason === 'error') process.exit(1);
'''


def main():
    """Keep real auth in controller memory, never write it to a fixture."""
    fiber_sandbox.probe()
    catalog = json.loads((Path.home() / '.pi/agent/models.json').read_text())
    for family, model in [('deepseek', 'deepseek-flash'), ('zai', 'sk-zai-m')]:
        provider = 'skgw-' + family
        key = catalog['providers'][provider]['apiKey']
        if re.fullmatch('[A-Z_][A-Z0-9_]*', key):
            key = os.environ[key]
        if not key or key.startswith('!'):
            raise ValueError('credential-reference-unavailable')
        with tempfile.TemporaryDirectory(prefix='fiber-sdk-') as td:
            root = Path(td)
            work, control = root / 'work', root / 'control'
            work.mkdir()
            fiber_sandbox.prepare_control(control, work, {'scope': 'synthetic gateway probe, no task execution'},
                                          fiber_sandbox.public_catalog(catalog, provider, model))
            (control / 'probe.mjs').write_text(SCRIPT)
            with gateway_socket(control / 'gateway.sock', endpoint='http://chiap01:18790/v1',
                                credential=key, family=family, model=model, check_claim=lambda: None) as server:
                command = fiber_sandbox.command(work, control, Path.home() / '.npm-global/lib/node_modules',
                    ['/usr/bin/python3', '/opt/fiber_gateway_bridge.py', fiber_sandbox.NODE, '/run/fleet/probe.mjs'])
                result = subprocess.run(command, env={'PATH': '/usr/bin:/bin'}, timeout=180)
                print(json.dumps({'provider': family, 'gateway_requests': server.requests, 'failures': server.failures}), flush=True)
                if result.returncode:
                    return result.returncode
    return 0


if __name__ == '__main__':
    sys.exit(main())
