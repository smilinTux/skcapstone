#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(check, message) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (await check()) return;
    await sleep(50);
  }
  throw new Error(message);
}

async function qualify() {
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-22-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-22-home-"));
  const bearerFile = path.join(home, "bearer");
  const port = 17883;
  const python = `
import importlib.util
from pathlib import Path
import uvicorn
repo = Path(${JSON.stringify(repo)})
spec = importlib.util.spec_from_file_location("decision_fixture", repo / "tests/test_control_plane_decision_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rig = module.Rig(target="/api/v1/reliability/projection")
Path(${JSON.stringify(bearerFile)}).write_text(rig.bearer)
legacy = {"incident_records":10,"incident_aliases":10,"problem_records":1,"problem_aliases":1,"change_records":2,"change_aliases":2}
def metric(mid, label, value, unit="percent", numerator=1, denominator=2, truth="current"):
    return {"metric_id":mid,"label":label,"value":value,"unit":unit,"truth_state":truth,"numerator":numerator,"denominator":denominator,"sample_size":denominator or 0,"window":"7d","classification":"fixture","exclusions":[],"legacy_coverage":legacy,"evidence_refs":["fixture://itil"]}
metrics = [
 metric("service.availability_sli","User-facing availability SLI",None,numerator=None,denominator=None,truth="unknown"),
 metric("service.slo_target","Approved service-level target",None,numerator=None,denominator=None,truth="unknown"),
 metric("service.error_budget_remaining","Error budget remaining",None,numerator=None,denominator=None,truth="unknown"),
 metric("itil.mtta_minutes","Mean time to acknowledge",5,"minutes",50,10),
 metric("itil.mttr_minutes","Mean time to resolve",20,"minutes",200,10),
 metric("itil.open_sla_breaches","Open incident response-target breaches",10,"incidents",10,10),
 metric("itil.incident_recurrence_rate","Problem-linked recurrence",100),
 metric("itil.change_lead_time_minutes","Closed change lead time",120,"minutes",240,2),
 metric("itil.change_success_rate","Change success rate",50),
 metric("itil.pir_coverage_rate","PIR evidence coverage",100,denominator=1),
 metric("itil.kedb_use_rate","Problem KEDB linkage",100,denominator=1),
]
projection = {"schema_version":"1.0.0","projection_id":"reliability-1","projection_hash":"sha256:"+"a"*64,"source_owner":"SKCapstone ITIL","scope":{},"observed_at":"2026-08-24T12:00:00Z","truth_state":"partial","visibility":{"state":"visible","authorization":"authorized"},"source_watermarks":[{"source":"fixture","value":"fixture-v1"}],"metrics":metrics,"items":{"incidents":[{"id":"inc-1","legacy_alias":"inc-1","title":"API unavailable","severity":"sev1","status":"investigating","services":["api"],"problem_id":"prb-1","detected_at":"2026-08-24T10:00:00Z","acknowledged_at":"2026-08-24T10:05:00Z","resolved_at":None}],"problems":[{"id":"prb-1","legacy_alias":"prb-1","title":"Recurring API","status":"known_error","incident_ids":["inc-1"],"kedb_id":"ke-1","change_id":"chg-1","workaround_recorded":True}],"changes":[{"id":"chg-1","legacy_alias":"chg-1","title":"Repair API","status":"closed","outcome":"successful","problem_id":"prb-1","validation":"passed","cab_required":True,"cab_votes":1,"scheduled":True,"deployed":True,"verified":True,"pir_recorded":True,"rollback_plan_recorded":True,"rollback_event_recorded":False}],"kedb":[{"id":"ke-1","title":"API known error","problem_id":"prb-1","change_id":"chg-1","root_cause_recorded":True,"workaround_recorded":True}],"breach_risk":[{"id":"inc-1","title":"API unavailable","severity":"sev1","remaining_min":-115,"over":True,"service":"api"}]},"display_limit":200,"record_counts":{"incidents":10,"problems":1,"changes":2,"kedb":1},"errors":[]}
class Provider:
    def read(self, context, query, home, *, currentness_verifier):
        assert currentness_verifier.check_before_owner_read(context).value == "allow"
        assert currentness_verifier.check_after_owner_read(context).value == "allow"
        return {**projection, "scope":query}
from skdashboard.dashboard import create_app
app = create_app(Path(${JSON.stringify(home)}), control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_reliability_provider=Provider())
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "inherit" });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  try {
    await waitFor(() => fs.existsSync(bearerFile), "Bearer fixture was not created");
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/reliability`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitFor(() => fs.existsSync(activePort), "Chrome did not publish DevToolsActivePort");
    const chromePort = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
    const targets = await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response) => response.json());
    const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let id = 0;
    const pending = new Map(), requests = [], exceptions = [];
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) { const handlers = pending.get(message.id); pending.delete(message.id); message.error ? handlers.reject(new Error(JSON.stringify(message.error))) : handlers.resolve(message.result); }
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => { id += 1; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
    const evaluate = async (expression) => { const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); assert.equal(result.exceptionDetails, undefined); return result.result.value; };
    const key = async (value, code, keyCode) => { await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: value, code, windowsVirtualKeyCode: keyCode }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code, windowsVirtualKeyCode: keyCode }); };
    await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable"); await send("Accessibility.enable");
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: `Bearer ${fs.readFileSync(bearerFile, "utf8")}`, Origin: "https://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/reliability?role=operator&scope=estate&window=latest&baseline=none&service=all` });
    await waitFor(async () => evaluate("document.querySelectorAll('#reliability-metric-rows tr').length === 11").catch(() => false), "Reliability metrics did not render");
    const view = JSON.parse(await evaluate("JSON.stringify({metrics:document.querySelectorAll('#reliability-metric-rows tr').length,breaches:document.querySelectorAll('#reliability-breach-rows tr').length,lineage:document.querySelectorAll('#reliability-lineage-rows tr').length,kedb:document.querySelectorAll('#reliability-kedb-rows tr').length,text:document.body.innerText})"));
    assert.equal(view.metrics, 11); assert.equal(view.breaches, 1); assert.equal(view.lineage, 2); assert.equal(view.kedb, 1);
    assert.match(view.text, /Approved service-level target\s+Unknown/i); assert.match(view.text, /10 incidents across 10 eligible/i); assert.match(view.text, /outcome successful/i); assert.match(view.text, /PIR Recorded/i);
    await evaluate("document.getElementById('reliability-role').focus()");
    await key("ArrowDown", "ArrowDown", 40); await key("Enter", "Enter", 13);
    await waitFor(async () => evaluate("new URL(location.href).searchParams.get('role') === 'architect'").catch(() => false), "Keyboard role change did not synchronize");
    for (const width of [390, 320]) { await send("Emulation.setDeviceMetricsOverride", { width, height: 800, deviceScaleFactor: 1, mobile: true }); const size = JSON.parse(await evaluate("JSON.stringify({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})")); assert.equal(size.scroll <= size.client, true, JSON.stringify(size)); }
    const writes = requests.filter((request) => !["GET", "OPTIONS"].includes(request.method));
    const external = requests.filter((request) => !request.url.startsWith(`http://127.0.0.1:${port}/`));
    assert.deepEqual(writes, []); assert.deepEqual(external, []); assert.deepEqual(exceptions, []);
    console.log("SKCP-22 CDP PASS: 11 metrics, breach denominator 10, lifecycle/PIR/KEDB, keyboard role, 390/320, zero writes/external/exceptions");
    socket.close();
  } finally { server.kill("SIGTERM"); chrome.kill("SIGTERM"); }
}

qualify().catch((error) => { console.error(error); process.exitCode = 1; });
