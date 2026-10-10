"""The worker's existing Pi extension must own transient gateway recovery."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_existing_worker_guard_registers_a_gateway_transport_hook():
    guard = ROOT / "scripts/fleet/pi-cardstore-guard.mjs"
    program = f"""
import assert from 'node:assert/strict';
import guard from {json.dumps(guard.as_uri())};
const hooks = new Map();
process.env.SKFLEET_LANE='codex';
guard({{on(name, hook) {{ hooks.set(name, hook); }}}});
assert.equal(typeof hooks.get('session_start'), 'function');
    assert.equal(hooks.has('turn_end'), false);
assert.equal(typeof hooks.get('tool_call'), 'function');
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def node_check(body):
    """Run deterministic transport contracts without Pi, credentials or real hosts."""
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    prefix = f"""
import assert from 'node:assert/strict';
import {{ gatewayTransport, wrapGatewayProvider }} from {json.dumps(helper.as_uri())};
const refused = (status, type='bucket_no_eligible_member') => new Response(
  JSON.stringify({{error:{{type,message:'synthetic response text'}}}}), {{status}});
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", prefix + body],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("status", [503, 504])
def test_transient_request_recovers_with_same_body_and_audited_backoff(status):
    node_check(f"""
let clock = 0;
const records=[], waits=[], requests=[];
const original = {{body:'synthetic request',headers:{{authorization:'synthetic fixture'}}}};
const t = gatewayTransport(async (url, options) => {{
  requests.push({{url,body:options.body,headers:options.headers}});
  return requests.length < 3 ? refused({status}) : new Response('ok');
}}, e=>records.push(e), {{now:()=>clock,wait:async ms=>{{waits.push(ms);clock+=ms;}}}});
const response = await t.fetch('http://fixture.invalid/completion',original);
assert.equal(response.status,200);
assert.equal(requests.length,3);
assert.deepEqual(waits,[10000,20000]);
assert(requests.every(r=>r.body===original.body && r.headers===original.headers));
assert.deepEqual(records.map(r=>r.event),['retry','retry','result']);
assert.equal(records[0].http_status,{status});
assert(!JSON.stringify(records).includes('synthetic request'));
assert(!JSON.stringify(records).includes('authorization'));
""")


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"type": "malformed_response", "code": "empty_upstream_response"}},
        {"error": {"code": "empty_upstream_response"}},
    ],
)
def test_malformed_upstream_502_retries_with_bounded_audit(payload):
    node_check(f"""
let calls=0;const records=[],waits=[];
const t=gatewayTransport(async()=>{{
  calls++;
  return calls<3
    ? new Response(JSON.stringify({json.dumps(payload)}),{{status:502}})
    : new Response('ok');
}},e=>records.push(e),{{wait:async ms=>waits.push(ms)}});
assert.equal((await t.fetch('http://fixture.invalid')).status,200);
assert.equal(calls,3);assert.deepEqual(waits,[10000,20000]);
assert.deepEqual(records.map(r=>r.event),['retry','retry','result']);
assert.equal(records[0].http_status,502);
""")


def test_malformed_upstream_502_is_bounded_to_four_attempts():
    node_check("""
let calls=0;const records=[],waits=[];
const response=()=>new Response(JSON.stringify({error:{
  type:'malformed_response',code:'empty_upstream_response'}}),{status:502});
const t=gatewayTransport(async()=>{calls++;return response();},e=>records.push(e),
  {wait:async ms=>waits.push(ms)});
assert.equal((await t.fetch('http://fixture.invalid')).status,502);
assert.equal(calls,4);assert.deepEqual(waits,[10000,20000,30000]);
assert.deepEqual(records.map(r=>r.attempt),[1,2,3,4]);
assert.equal(records[0].code,'empty_upstream_response');
assert.equal(records[0].type,'malformed_response');
assert.equal(t.state.terminal.retryable,false);
""")


def test_untyped_upstream_502_retries_then_recovers():
    node_check("""
let calls=0,clock=0;const waits=[];
const t=gatewayTransport(async()=>{
 calls++;
 return calls<3
  ? new Response(JSON.stringify({error:{type:'bad_gateway',code:'upstream_unavailable'}}),
     {status:502})
  : new Response('ok');
},()=>{},{now:()=>clock,wait:async ms=>{waits.push(ms);clock+=ms;}});
assert.equal((await t.fetch('http://fixture.invalid')).status,200);
assert.equal(calls,3);assert.deepEqual(waits,[10000,20000]);
""")


def test_untyped_502_is_bounded_to_four_attempts():
    node_check("""
let calls=0,clock=0;
const t=gatewayTransport(async()=>{calls++;return new Response('upstream down',{status:502});},
 ()=>{},{now:()=>clock,wait:async ms=>{clock+=ms;}});
assert.equal((await t.fetch('http://fixture.invalid')).status,502);
assert.equal(calls,4);assert.equal(t.state.terminal.retryable,false);
""")


def test_request_too_large_502_is_terminal_without_retry():
    node_check("""
let calls=0;
const t=gatewayTransport(async()=>{
 calls++;
 return new Response(JSON.stringify({error:{code:'request_too_large',param:'body'}}),
  {status:502});
},()=>{},{wait:()=>assert.fail('oversized retry')});
assert.equal((await t.fetch('http://fixture.invalid')).status,502);
assert.equal(calls,1);
""")


def test_four_attempts_maximum_then_terminal_with_full_retry_evidence():
    node_check("""
let clock=0,calls=0;const records=[],waits=[];
const t=gatewayTransport(async()=>{calls++;return refused(503);},e=>records.push(e),
  {now:()=>clock,wait:async ms=>{waits.push(ms);clock+=ms;}});
assert.equal((await t.fetch('http://fixture.invalid')).status,503);
assert.equal(calls,4);assert.deepEqual(waits,[10000,20000,30000]);
assert.deepEqual(records.map(r=>r.attempt),[1,2,3,4]);
assert.equal(t.state.terminal.retryable,false);
""")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 413])
def test_every_client_error_is_terminal_without_wait(status):
    node_check(f"""
let calls=0;const records=[];
const t=gatewayTransport(async()=>{{calls++;return refused({status});}},e=>records.push(e),
  {{wait:()=>assert.fail('4xx backoff')}});
assert.equal((await t.fetch('http://fixture.invalid')).status,{status});
assert.equal(calls,1);assert.equal(records[0].event,'result');
assert.equal(t.state.terminal.http_status,{status});
""")


@pytest.mark.parametrize(
    "payload", ["not json", '{"error":{"type":"other_failure"}}', "x" * 70000]
)
def test_unrelated_or_malformed_503_does_not_retry(payload):
    node_check(f"""
let calls=0;const t=gatewayTransport(async()=>{{
 calls++;return new Response({json.dumps(payload)},{{status:503}});}},
 ()=>{{}},{{wait:()=>assert.fail('unqualified503 retry')}});
assert.equal((await t.fetch('http://fixture.invalid')).status,503);assert.equal(calls,1);
""")


def test_budget_prevents_another_attempt_after_slow_gateway_requests():
    node_check("""
let clock=0,calls=0;const records=[];
const t=gatewayTransport(async()=>{calls++;clock+=180000;return refused(504);},e=>records.push(e),
 {now:()=>clock,wait:async ms=>{clock+=ms;}});
await t.fetch('http://fixture.invalid');
assert.equal(calls,2);assert.equal(records.at(-1).event,'result');
assert.equal(t.state.terminal.retryable,false);
""")


def test_retry_budget_reserves_a_final_attempt_inside_pi_deadline():
    node_check("""
let clock=0,calls=0;const records=[],waits=[];
const durations=[180000,30000,60000,1000];
const t=gatewayTransport(async()=>{
  const duration=durations[calls++];clock+=duration;
  return calls<4?refused(calls===1?504:503):new Response('ok');
},e=>records.push(e),{now:()=>clock,wait:async ms=>{waits.push(ms);clock+=ms;}});
const response=await t.fetch('http://fixture.invalid');
assert.equal(response.status,200);
assert.equal(calls,4);
assert.deepEqual(waits,[10000,20000,0]);
assert.deepEqual(records.map(r=>r.http_status),[504,503,503,200]);
assert.deepEqual(records.map(r=>r.event),['retry','retry','retry','result']);
""")


def test_glm_lane_gets_600_second_request_window_and_570_second_retry_budget():
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    program = f"""
import assert from 'node:assert/strict';
import gatewayRetry, {{
  gatewayTransport, requestPolicyForLane,
}} from {json.dumps(helper.as_uri())};
let hooks=new Map(),configured;
process.env.SKFLEET_LANE='glm';
assert.deepEqual(requestPolicyForLane('glm'),{{budgetMs:570000,idleTimeoutMs:600000}});
assert.deepEqual(requestPolicyForLane('codex'),{{budgetMs:360000,idleTimeoutMs:390000}});
gatewayRetry({{on:(name,fn)=>hooks.set(name,fn),registerProvider:()=>{{}},
 appendEntry:()=>{{}}}},{{configureIdleTimeout:async options=>{{configured=options.timeoutMs;}}}});
const original={{id:'skgateway',streamSimple(){{}}}};
await hooks.get('session_start')({{}},{{modelRegistry:{{getProvider:()=>original}}}});
assert.equal(configured,600000);
let clock=0,calls=0;const delays=[];
const transport=gatewayTransport(async()=>{{
 calls++;clock+=calls===1?216000:calls===2?144000:180000;
 return new Response('slow',{{status:504}});
}},()=>{{}},{{now:()=>clock,wait:async ms=>{{delays.push(ms);clock+=ms;}},budgetMs:570000}});
assert.equal((await transport.fetch('http://fixture.invalid')).status,504);
assert.equal(calls,3);
assert.deepEqual(delays,[10000,20000]);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_glm_leaves_compaction_and_resume_to_pi():
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    program = f"""
import assert from 'node:assert/strict';
import gatewayRetry from {json.dumps(helper.as_uri())};
const hooks=new Map();let continuations=0;
process.env.SKFLEET_LANE='glm';
gatewayRetry({{
 on:(name,fn)=>hooks.set(name,fn),
 sendUserMessage:()=>continuations++,
 registerProvider:()=>{{}},appendEntry:()=>{{}},
}});
assert.equal(hooks.has('turn_end'),false);
// Even an over-threshold GLM session cannot trigger a detached compaction
// after turn_end; Pi's native compaction runs inside and resumes the run.
assert.equal(continuations,0);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("model_id", ["sk-glm-s", "sk-glm-m", "sk-glm-l", "glm-4.5", "glm-5.3"])
def test_glm_model_context_is_capped_before_native_compaction(model_id):
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    program = f"""
import assert from 'node:assert/strict';
import gatewayRetry from {json.dumps(helper.as_uri())};
const hooks=new Map();let selected;
process.env.SKFLEET_LANE='glm';
gatewayRetry({{
 on:(name,fn)=>hooks.set(name,fn),
 setModel:async model=>{{selected=model;return true;}},
 registerProvider:()=>{{}},appendEntry:()=>{{}},
}},{{configureIdleTimeout:async()=>{{}}}});
await hooks.get('session_start')({{}},{{
 model:{{id:{json.dumps(model_id)},provider:'skgateway',contextWindow:200000}},
 modelRegistry:{{getProvider:()=>({{id:'skgateway',streamSimple(){{}}}})}},
}});
assert.equal(selected.id,{json.dumps(model_id)});
assert.equal(selected.contextWindow,128000);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_non_glm_lane_keeps_existing_timeout_and_does_not_auto_compact():
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    program = f"""
import assert from 'node:assert/strict';
import gatewayRetry from {json.dumps(helper.as_uri())};
const hooks=new Map();let configured;
process.env.SKFLEET_LANE='codex';
gatewayRetry({{on:(name,fn)=>hooks.set(name,fn),registerProvider:()=>{{}},appendEntry:()=>{{}}}},
 {{configureIdleTimeout:async options=>{{configured=options.timeoutMs;}}}});
const original={{id:'skgateway',streamSimple(){{}}}};
await hooks.get('session_start')({{}},{{modelRegistry:{{getProvider:()=>original}}}});
assert.equal(configured,390000);
assert.equal(hooks.has('turn_end'),false);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_abort_during_backoff_preserves_cancellation_and_makes_no_second_call():
    node_check("""
const abort=new AbortController();let calls=0;const records=[];
const t=gatewayTransport(async()=>{calls++;return refused(503);},e=>records.push(e),
 {wait:async()=>{abort.abort();throw new DOMException('cancelled','AbortError');}});
await assert.rejects(t.fetch('http://fixture.invalid',{signal:abort.signal}),{name:'AbortError'});
assert.equal(calls,1);assert.equal(records.at(-1).reason,'cancelled');
""")


def test_terminal_client_error_does_not_enter_pi_outer_retry_round():
    node_check("""
let got;const records=[];const provider={id:'skgateway',auth:{apiKey:'fixture'},
 getModels:()=>['unchanged'],streamSimple(model,context,options){
  got=options;
  const result=(async()=>{await options.fetch('http://fixture.invalid');return {
   stopReason:'error',errorMessage:'401: unauthorized',content:[]};})();
  return {result:()=>result,async *[Symbol.asyncIterator]() {
    yield {type:'error',error:await result};}};
 }};
const wrapped=wrapGatewayProvider(provider,e=>records.push(e));
assert.equal(wrapped.auth,provider.auth);assert.equal(wrapped.getModels,provider.getModels);
const stream=wrapped.streamSimple({provider:'skgateway',id:'sk-glm-m'},{},
 {fetch:async()=>refused(401),maxRetries:99});
const events=[];for await(const e of stream)events.push(e);
const result=await stream.result();
assert.equal(got.maxRetries,0);assert.equal(result.gatewayError.http_status,401);
assert(!/401|unauthorized|503|504/.test(result.errorMessage));
assert.equal(events[0].error,result);assert.equal(records[0].provider,'skgateway');
""")


def test_partial_stream_failure_is_not_replayed():
    node_check("""
const message={stopReason:'error',errorMessage:'504: server error',content:['partial']};
const provider={id:'skgateway',streamSimple(){return {
 async *[Symbol.asyncIterator](){yield {type:'text_delta',delta:'partial'};
   yield {type:'error',error:message};},
 result:async()=>message};}};
const stream=wrapGatewayProvider(provider,()=>{}).streamSimple(
 {provider:'skgateway',id:'sk-glm-m'},{});
for await(const event of stream){}
assert.equal((await stream.result()).gatewayError.reason,'partial_stream');
assert(!message.errorMessage.includes('504'));
""")


def test_hook_preserves_native_provider_metadata_and_ships_beside_guard():
    import tomllib

    package = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "scripts/fleet/pi-gateway-retry.mjs" in package["tool"]["setuptools"]["script-files"]
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    program = f"""
import assert from 'node:assert/strict';
import gatewayRetry from {json.dumps(helper.as_uri())};
let hooks=new Map(),registered,configured;
process.env.SKFLEET_LANE='codex';
const original={{id:'skgateway',auth:{{apiKey:'synthetic'}},baseUrl:'http://fixture.invalid',
 getModels:()=>['original'],streamSimple:()=>{{}}}};
 gatewayRetry({{on(name,fn){{hooks.set(name,fn);}},
 registerProvider(p){{registered=p;}},appendEntry(){{}}}},
 {{configureIdleTimeout:async()=>{{configured=390000;}}}});
await hooks.get('session_start')({{}},{{modelRegistry:{{getProvider(name){{
 assert.equal(name,'skgateway');return original;}}}}}});
assert.equal(registered.auth,original.auth);
assert.equal(registered.baseUrl,original.baseUrl);
assert.equal(registered.getModels,original.getModels);
assert.notEqual(registered.streamSimple,original.streamSimple);
assert.equal(configured,390000);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_pi_idle_timeout_configures_running_entrypoint_dispatcher():
    helper = ROOT / "scripts/fleet/pi-gateway-retry.mjs"
    program = f"""
import assert from 'node:assert/strict';
import {{ configurePiHttpIdleTimeout }} from {json.dumps(helper.as_uri())};
let imported,configured;
await configurePiHttpIdleTimeout({{entrypoint:process.execPath,importer:async specifier=>{{
 imported=specifier;return {{configureHttpDispatcher:ms=>{{configured=ms;}}}};
}}}});
assert(imported.startsWith('file:'));
assert(imported.endsWith('/core/http-dispatcher.js'));
assert.equal(configured,390000);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", program],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_gateway_429_is_retried_with_backoff():
    """ae812f7a: a pool-full 429 ended a nearly finished review on attempt 1."""
    node_check("""
let calls=0;const records=[],waits=[];
const t=gatewayTransport(async()=>{calls++;return calls<3?refused(429,'pool_full'):new Response('ok');},
  e=>records.push(e),{wait:async ms=>waits.push(ms)});
assert.equal((await t.fetch('http://fixture.invalid')).status,200);
assert.equal(calls,3);assert.deepEqual(waits,[30000,60000]);
assert.deepEqual(records.map(r=>r.event),['retry','retry','result']);
""")


def test_gateway_429_outlasts_backend_cooldowns_within_budget():
    """477886d2: four 429s inside back-to-back 30s cooldowns killed a build."""
    node_check("""
let calls=0;let clock=0;const waits=[];
const t=gatewayTransport(async()=>{calls++;return calls<7?refused(429):new Response('ok');},
  ()=>{},{now:()=>clock,wait:async ms=>{waits.push(ms);clock+=ms;},budgetMs:570000});
assert.equal((await t.fetch('http://fixture.invalid')).status,200);
assert.equal(calls,7);assert.deepEqual(waits,[30000,60000,90000,120000,120000,0]);
""")


def test_gateway_429_honors_a_bounded_retry_after():
    node_check("""
let calls=0;const waits=[];
const limited=s=>new Response('{}',{status:429,headers:{'retry-after':String(s)}});
const t=gatewayTransport(async()=>{calls++;return calls===1?limited(45):calls===2?limited(9999):new Response('ok');},
  ()=>{},{wait:async ms=>waits.push(ms)});
assert.equal((await t.fetch('http://fixture.invalid')).status,200);
assert.deepEqual(waits,[45000,60000]);
""")
