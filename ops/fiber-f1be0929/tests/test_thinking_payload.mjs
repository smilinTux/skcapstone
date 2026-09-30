// Offline qualification of the actual installed SDK; no credential or network use.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { streamSimple } = await import(pathToFileURL(process.argv[2]).href);
const catalog = JSON.parse(readFileSync(new URL('../approved-aliases.json', import.meta.url)));
let checked = 0;
for (const [provider, id] of [['skgw-deepseek', 'deepseek-flash'], ['skgw-zai', 'sk-zai-m']]) {
  const definition = catalog.providers[provider];
  const entry = definition.models.find(model => model.id === id);
  const model = { ...entry, provider, api: definition.api,
    baseUrl: 'http://127.0.0.1:1/v1', compat: { ...definition.compat, ...entry.compat } };
  for (const reasoning of ['off', 'high']) {
    let captured;
    await streamSimple(model, { messages: [{ role: 'user', content: 'Reply OK.', timestamp: 1 }] }, {
      apiKey: 'synthetic-never-sent', reasoning,
      onPayload(value) { captured = value; throw Error('offline-payload-captured'); },
      fetch() { throw Error('NETWORK_FORBIDDEN'); },
    }).result();
    assert.ok(captured, 'SDK payload must be captured before HTTP');
    if (provider === 'skgw-deepseek') {
      assert.deepEqual(captured.thinking, { type: reasoning === 'off' ? 'disabled' : 'enabled' });
      assert.equal(captured.reasoning_effort, reasoning === 'off' ? undefined : 'high');
    } else {
      assert.equal(captured.thinking, undefined, 'GLM-5.3 must not receive disabled thinking');
      assert.equal(captured.reasoning_effort, reasoning === 'off' ? 'low' : 'high');
    }
    checked++;
  }
}
console.log(JSON.stringify({ passed: checked, network_used: false }));
