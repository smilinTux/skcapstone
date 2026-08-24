#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(check, message) { for (let attempt = 0; attempt < 200; attempt += 1) { if (await check()) return; await sleep(50); } throw new Error(message); }

async function qualify() {
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-32-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-32-home-"));
  const bearerFile = path.join(home, "bearer");
  const fixtureFile = path.join(home, "fixture.json");
  const port = 17886;
  const python = `
import importlib.util
from pathlib import Path
import uvicorn
repo=Path(${JSON.stringify(repo)})
spec=importlib.util.spec_from_file_location("decision_fixture", repo/"tests/test_control_plane_decision_context.py")
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
rig=module.Rig(target="/api/v1/reports/projection")
Path(${JSON.stringify(bearerFile)}).write_text(rig.bearer)
home=Path(${JSON.stringify(home)})
from skdashboard.dashboard_reports import ReportSnapshotStore, build_report_snapshot

def metric(value, truth="current"):
 return {"metric_id":"flow.review_coverage","schema_version":"1.1.0","definition_version":"1.0.0","label":"Review coverage","value":value,"unit":"percent","polarity":"higher_is_better","numerator":value if value is not None else None,"denominator":100 if value is not None else None,"sample_size":10 if value is not None else None,"scope":{"portfolio_id":"estate"},"grain":"estate","window":{"start":"2026-08-17T00:00:00Z","end":"2026-08-24T00:00:00Z","timezone":"UTC","baseline":"previous"},"target":None,"truth_state":truth,"visibility":{"state":"visible","authorization":"authorized"},"measurement_kind":"derived","confidence":None,"source":{"owner":"skcoord","adapter_id":"skcoord.flow","adapter_version":"1.0.0","observed_at":"2026-08-24T00:00:00Z","projected_at":"2026-08-24T00:01:00Z","freshness_ttl_seconds":300,"watermarks":[{"source":"skcoord.flow","value":"sha256:"+"a"*64}],"evidence_refs":["evidence:flow"]},"data_quality":{"coverage_numerator":10,"coverage_denominator":10,"errors":[] if truth=="current" else ["source unavailable"],"exclusions":["individual activity"],"notes":[]},"calculation":{"definition_hash":"sha256:"+"b"*64,"method":"ratio_percent","expression":"100 * numerator / denominator","calculation_ref":"registry:1.0.0:flow.review_coverage@1.0.0"},"classification":{"level":"internal","policy_decision_ref":None,"purpose":"control_plane_reporting"}}

def insight():
 return {"insight_id":"ins-report-narrative","schema_version":"1.1.0","status":"proposal","kind":"report_narrative","summary":"Evidence-linked summary.","scope":{"portfolio_id":"estate"},"window":{"start":"2026-08-17T00:00:00Z","end":"2026-08-24T00:00:00Z","timezone":"UTC","baseline":"previous"},"metric_refs":["flow.review_coverage@1.0.0"],"evidence_refs":["evidence:flow"],"calculation_refs":["registry:1.0.0:flow.review_coverage@1.0.0"],"uncertainty":["One source window."],"contradictions":[],"exclusions":["individual activity"],"visibility":{"state":"visible","authorization":"authorized"},"model_provenance":{"logical_route":"skdashboard.reporting","transport_profile":"fixture","gateway_revision":"fixture-r1","backend":"fixture","requested_model":"fixture-model","served_model":"fixture-model","model_revision":"fixture-r1","prompt_hash":"sha256:"+"c"*64,"schema_hash":"sha256:"+"d"*64},"policy_decision_ref":"decision-fixture","recommendations":[],"next_steps":[{"label":"Open evidence","kind":"open_evidence","preview_only":True,"target_ref":"evidence:flow"}]}
store=ReportSnapshotStore(home)
first=build_report_snapshot(report_type="weekly_portfolio",audience=["portfolio review"],generated_at="2026-08-24T00:02:00Z",as_of="2026-08-24T00:00:00Z",scope={"portfolio_id":"estate"},baseline="previous",sections=[{"section_id":"portfolio","title":"Portfolio","metric_results":[metric(4)],"insights":[insight()]}])
store.put(first)
second=build_report_snapshot(report_type="weekly_portfolio",audience=["portfolio review"],generated_at="2026-08-24T00:03:00Z",as_of="2026-08-24T00:00:00Z",scope={"portfolio_id":"estate"},baseline="previous",sections=[{"section_id":"portfolio","title":"Portfolio","metric_results":[metric(None,"unavailable")],"insights":[]}],supersedes=first["snapshot_id"])
store.put(second)
Path(${JSON.stringify(fixtureFile)}).write_text(__import__("json").dumps({"original":first["snapshot_id"],"superseding":second["snapshot_id"]}))
from skdashboard.dashboard import create_app
app=create_app(home,control_plane_decision_authorizer=rig.authorizer,control_plane_invocation_factory=rig.factory)
uvicorn.run(app,host="127.0.0.1",port=${port},log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "ignore" });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  try {
    await waitFor(() => fs.existsSync(bearerFile) && fs.existsSync(fixtureFile), "Report fixture was not created");
    const fixture = JSON.parse(fs.readFileSync(fixtureFile, "utf8"));
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/reports`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
    const activePort=path.join(profile,"DevToolsActivePort"); await waitFor(()=>fs.existsSync(activePort),"Chrome did not publish DevToolsActivePort");
    const chromePort=fs.readFileSync(activePort,"utf8").trim().split("\n")[0]; const targets=await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response)=>response.json());
    const socket=new WebSocket(targets.find((target)=>target.type==="page").webSocketDebuggerUrl); await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
    let id=0; const pending=new Map(),requests=[],exceptions=[];
    socket.onmessage=(event)=>{const message=JSON.parse(event.data);if(message.id&&pending.has(message.id)){const handlers=pending.get(message.id);pending.delete(message.id);message.error?handlers.reject(new Error(JSON.stringify(message.error))):handlers.resolve(message.result);}if(message.method==="Network.requestWillBeSent")requests.push(message.params.request);if(message.method==="Runtime.exceptionThrown")exceptions.push(message.params.exceptionDetails);};
    const send=(method,params={})=>new Promise((resolve,reject)=>{id+=1;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
    const evaluate=async(expression)=>{const result=await send("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});assert.equal(result.exceptionDetails,undefined);return result.result.value;};
    const key=async(value,code,keyCode)=>{await send("Input.dispatchKeyEvent",{type:"rawKeyDown",key:value,code,windowsVirtualKeyCode:keyCode});await send("Input.dispatchKeyEvent",{type:"keyUp",key:value,code,windowsVirtualKeyCode:keyCode});};
    await send("Page.enable");await send("Runtime.enable");await send("Network.enable");await send("Accessibility.enable");await send("Network.setExtraHTTPHeaders",{headers:{Authorization:`Bearer ${fs.readFileSync(bearerFile,"utf8")}`,Origin:"https://10.0.0.139:7778"}});
    await send("Page.navigate",{url:`http://127.0.0.1:${port}/control-plane/reports?role=project-manager&scope=estate&window=latest&baseline=none&service=all&report_type=all&snapshot=${fixture.original}&compare=${fixture.superseding}`});
    await waitFor(async()=>evaluate("document.querySelectorAll('#snapshot-rows tr').length === 2").catch(()=>false),"Report rows did not render");
    let view=JSON.parse(await evaluate("JSON.stringify({reports:document.querySelectorAll('#snapshot-rows tr').length,metrics:document.querySelectorAll('#metric-rows tr').length,text:document.body.innerText})"));
    assert.equal(view.reports,2);assert.equal(view.metrics,1);assert.match(view.text,/unavailable/i);assert.match(view.text,/supersession/i);assert.match(view.text,/skdashboard.reporting/i);
    assert.equal(await evaluate("document.getElementById('comparison-state').textContent"),"comparable");
    assert.match(await evaluate("document.getElementById('comparison-rows').innerText"),/false[\s\S]*Not comparable/i);
    for(const width of [390,320]){await send("Emulation.setDeviceMetricsOverride",{width,height:800,deviceScaleFactor:1,mobile:true});const size=JSON.parse(await evaluate("JSON.stringify({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})"));assert.equal(size.scroll<=size.client,true,JSON.stringify(size));}
    await evaluate("document.getElementById('reports-role').focus()");await key("ArrowDown","ArrowDown",40);await key("Enter","Enter",13);await waitFor(async()=>evaluate("new URL(location.href).searchParams.get('role') === 'operator'").catch(()=>false),"Keyboard role change did not synchronize");
    const writes=requests.filter((request)=>!["GET","OPTIONS"].includes(request.method));const external=requests.filter((request)=>!request.url.startsWith(`http://127.0.0.1:${port}/`)&&!request.url.startsWith("data:"));
    assert.deepEqual(writes,[]);assert.deepEqual(external,[]);assert.deepEqual(exceptions,[]);
    console.log("SKCP-32 CDP PASS: 2 immutable reports, supersession, typed AI provenance, no-value comparison, keyboard, 390/320, zero writes/external/exceptions");socket.close();
  } finally {server.kill("SIGTERM");chrome.kill("SIGTERM");}
}
qualify().catch((error)=>{console.error(error);process.exitCode=1;});
