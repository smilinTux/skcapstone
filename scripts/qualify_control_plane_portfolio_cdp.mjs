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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-21-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-21-home-"));
  const bearerFile = path.join(home, "bearer");
  const port = 17881;
  const python = `
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
import uvicorn
repo = Path(${JSON.stringify(repo)})
spec = importlib.util.spec_from_file_location("decision_fixture", repo / "tests/test_control_plane_decision_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rig = module.Rig()
Path(${JSON.stringify(bearerFile)}).write_text(rig.bearer)
portfolio = {"adapter_id":"skcapstone.portfolio","adapter_version":"1.0.0","owner":"SKCapstone","population":"portfolio_project_work","truth_state":"current","aggregate":{"total":2,"open":2,"in_progress":1,"done":0},"coverage":{"reporting":2,"expected":2},"errors":[],"watermark":{"value":"portfolio-r1"},"observed_at":"2026-08-24T00:00:00Z"}
flow = {"adapter_id":"skcoord.flow","adapter_version":"1.0.0","owner":"SKCoord","population":"task_flow","truth_state":"current","aggregate":{"in_progress":1,"blocked":0},"coverage":{"reporting":2,"expected":2},"errors":[],"watermark":{"value":"flow-r1"},"observed_at":"2026-08-24T00:00:00Z"}
project = {"projection_type":"project_records","schema_version":"1.0.0","source_owner":"skcoord","source_model":"AuthorizedCardSnapshotReader","classification":"internal","scope":{"role":"operator","scope":"estate","service":"all","window":"latest","baseline":"none"},"policy_decision":{"owner_policy_revision":"a"*64,"visible_set_sha256":"sha256:"+"b"*64},"visibility":{"state":"visible","authorization":"authorized"},"truth_state":"partial","snapshot_consistency":"per_authorized_record_fold","observed_at":"2026-08-24T00:00:00Z","projected_at":"2026-08-24T00:00:00Z","watermark":{"source":"skcoord.authorized_card_snapshot","value":"sha256:"+"c"*64},"population_counts":{"authorized_ids":2,"folded":2,"emitted_records":2,"visible_edges":1,"attested_orphan_edges":0,"emitted_findings":1,"explicit_milestones":1,"emitted_milestones":1},"classification_complete":True,"truncated":True,"records":[{"record_id":"project-1","source_ref":"card:project-1","kind":"task","classifications":["project"],"status":"doing","priority":"high","owner":"owner-ref","created_at":"2026-08-01T00:00:00Z","updated_at":"2026-08-24T00:00:00Z","archived":False,"visible_dependency_count":1,"folded_conflict_evidence":False},{"record_id":"gate-1","source_ref":"card:gate-1","kind":"task","classifications":["human-gate","milestone"],"status":"backlog","priority":"critical","owner":"human-owner","created_at":"2026-08-01T00:00:00Z","updated_at":"2026-08-24T00:00:00Z","archived":False,"visible_dependency_count":0,"folded_conflict_evidence":False}],"dependency_edges":[{"from_record_id":"project-1","to_record_id":"gate-1","resolution":"open","conditions":["human_gated","milestone_path"],"source_owner":"owner-ref","target_owner":"human-owner","target_status":"backlog","stale_rule":"dependency-unresolved-30d@1.0.0","stale_record_refs":[],"freshness_unknown_record_refs":[],"path_record_ids":["gate-1"],"evidence_refs":["project-1","gate-1"]}],"milestones":[{"record_id":"gate-1","source_ref":"card:gate-1","kind":"task","classifications":["human-gate","milestone"],"status":"backlog","priority":"critical","owner":"human-owner","created_at":"2026-08-01T00:00:00Z","updated_at":"2026-08-24T00:00:00Z","archived":False,"visible_dependency_count":0,"folded_conflict_evidence":False,"dependency_path_summary":{"findings":0,"conditions":{},"path_record_ids":["gate-1"],"partial":False}}],"errors":[]}
class Provider:
    def read(self, context, scope, home, *, currentness_verifier):
        assert currentness_verifier.check_before_owner_read(context).value == "allow"
        assert currentness_verifier.check_after_owner_read(context).value == "allow"
        return {**project, "scope": scope.model_dump(mode="json")}
patch("skdashboard.control_plane_adapters.default_readers", return_value={}).start()
patch("skdashboard.control_plane_adapters.project_estate", return_value=[portfolio, flow]).start()
patch("skdashboard.control_plane_quality.project_data_quality", return_value={"projection_type":"data_quality","truth_state":"current"}).start()
from skdashboard.dashboard import create_app
app = create_app(Path(${JSON.stringify(home)}), control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_project_provider=Provider())
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "ignore" });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  try {
    await waitFor(() => fs.existsSync(bearerFile), "Bearer fixture was not created");
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/portfolio`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitFor(() => fs.existsSync(activePort), "Chrome did not publish DevToolsActivePort");
    const chromePort = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
    const targets = await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response) => response.json());
    const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let id = 0;
    const pending = new Map();
    const requests = [];
    const exceptions = [];
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const handlers = pending.get(message.id); pending.delete(message.id);
        if (message.error) handlers.reject(new Error(JSON.stringify(message.error))); else handlers.resolve(message.result);
      }
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => { id += 1; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
    const evaluate = async (expression) => {
      const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      assert.equal(result.exceptionDetails, undefined);
      return result.result.value;
    };
    const key = async (value, code = value) => {
      const keyCode = { Enter: 13, Escape: 27, Tab: 9 }[value] || 0;
      await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: value, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode });
    };
    await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable"); await send("Accessibility.enable");
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: `Bearer ${fs.readFileSync(bearerFile, "utf8")}`, Origin: "http://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/portfolio?role=operator&scope=estate&window=latest&baseline=none&service=all` });
    await waitFor(async () => evaluate("document.querySelectorAll('#project-rows tr[data-signal]').length >= 30").catch(() => false), "Portfolio signals did not render");
    const view = JSON.parse(await evaluate(`JSON.stringify({signals:document.querySelectorAll('#project-rows tr[data-signal]').length,records:document.querySelectorAll('#record-rows tr').length,edges:document.querySelectorAll('#dependency-rows tr').length,milestones:document.querySelectorAll('#milestone-rows tr').length,text:document.body.innerText})`));
    assert.ok(view.signals >= 30); assert.equal(view.records, 2); assert.equal(view.edges, 1); assert.equal(view.milestones, 1);
    assert.match(view.text, /project-1/); assert.match(view.text, /human gated/i); assert.match(view.text, /HISTORICAL FLOW METRICS\s+Unknown/i); assert.match(view.text, /Observed 1 in emitted subset/);
    await evaluate("document.querySelector('.project-evidence-button').focus()"); await key("Enter");
    assert.equal(await evaluate("document.getElementById('project-evidence').open"), true);
    await key("Escape");
    assert.equal(await evaluate("document.activeElement.classList.contains('project-evidence-button')"), true);
    for (const width of [390, 320]) {
      await send("Emulation.setDeviceMetricsOverride", { width, height: 800, deviceScaleFactor: 1, mobile: true });
      const sizing = JSON.parse(await evaluate("JSON.stringify({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})"));
      assert.equal(sizing.scroll <= sizing.client, true, JSON.stringify(sizing));
    }
    const writes = requests.filter((request) => !["GET", "OPTIONS"].includes(request.method));
    const external = requests.filter((request) => !request.url.startsWith(`http://127.0.0.1:${port}/`));
    assert.deepEqual(writes, []); assert.deepEqual(external, []); assert.deepEqual(exceptions, []);
    console.log(`SKCP-21 CDP PASS: ${view.signals} signals, 2 records, 1 edge, 1 milestone, keyboard evidence, 390/320, zero writes/external/exceptions`);
    socket.close();
  } finally {
    server.kill("SIGTERM"); chrome.kill("SIGTERM");
  }
}

qualify().catch((error) => { console.error(error); process.exitCode = 1; });
