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
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-21a-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-21a-home-"));
  const bearerFile = path.join(home, "bearer");
  const port = 17882;
  const python = `
import importlib.util
from pathlib import Path
import uvicorn
repo = Path(${JSON.stringify(repo)})
spec = importlib.util.spec_from_file_location("decision_fixture", repo / "tests/test_control_plane_decision_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rig = module.Rig(target="/api/v1/schedule/projection")
Path(${JSON.stringify(bearerFile)}).write_text(rig.bearer)
known = {"state":"known","instant":"2026-08-24T12:00:00Z"}
unknown = {"state":"unknown","instant":None,"reason":"owner date unavailable"}
na = {"state":"not_applicable","instant":None,"reason":"not applicable"}
def item(item_id, title, item_type, target, truth="current"):
    return {"item_id":item_id,"title":title,"item_type":item_type,"owner_service_id":"skcoord","service_id":"skdashboard","status":"doing","truth_state":truth,"visibility":{"state":"visible","authorization":"authorized"},"dates":{"baseline_start":known,"baseline_target":target,"planned_start":known,"planned_target":target,"actual_start":unknown,"actual_finish":na},"baseline_variance":{"state":"known","seconds":0},"progress":0.5,"progress_basis":"visible eligible children","rollup":{"state":"complete","eligible_children":1,"included_children":1,"start":known,"end":target,"progress":0.5,"progress_basis":"visible eligible children","exclusions":[]},"source_watermarks":[{"source":"fixture","value":"fixture-v1"}],"evidence_refs":["evidence://schedule/"+item_id]}
projection = {"schema_version":"1.0.0","projection_id":"schedule-1","projection_version":"projection-v1","projection_hash":"sha256:"+"a"*64,"scope":{"role":"project_manager","service_id":"all"},"display_timezone":"UTC","observed_at":"2026-08-24T12:00:00Z","projected_at":"2026-08-24T12:01:00Z","truth_state":"partial","visibility":{"state":"visible","authorization":"authorized"},"source_watermarks":[{"source":"fixture","value":"fixture-v1"}],"items":[item("project-1","Portfolio workspace","project",{"state":"known","instant":"2026-08-28T12:00:00Z"}),item("milestone-1","Release milestone","milestone",{"state":"known","instant":"2026-08-30T12:00:00Z"},"partial")],"dependencies":[{"dependency_id":"dependency-1","source_item_id":"project-1","target_item_id":"milestone-1","edge_type":"finish_to_start","direction":"known","lag_seconds":0,"truth_state":"current","visibility":{"state":"visible","authorization":"authorized"},"blocker_state":"blocking","cycle_state":"acyclic","evidence_refs":["evidence://dependency/1"]}],"overlays":[{"overlay_id":"blackout-1","overlay_type":"blackout","owner_service_id":"skcapstone","start":known,"end":{"state":"known","instant":"2026-08-29T12:00:00Z"},"truth_state":"current","visibility":{"state":"visible","authorization":"authorized"},"conflict_state":"conflict","evidence_refs":["evidence://blackout/1"]}],"cycle_analysis":{"state":"acyclic","cycle_item_ids":[],"evidence_refs":[]},"critical_path":{"state":"unavailable","item_ids":[],"reasons":["conflicting_blackout"]},"individual_ranking_prohibited":True,"errors":["blackout conflict"]}
class Provider:
    def read(self, context, query, home, *, currentness_verifier):
        assert currentness_verifier.check_before_owner_read(context).value == "allow"
        assert currentness_verifier.check_after_owner_read(context).value == "allow"
        return {**projection, "display_timezone":query["timezone"]}
from skdashboard.dashboard import create_app
app = create_app(Path(${JSON.stringify(home)}), control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_schedule_provider=Provider())
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "ignore" });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  try {
    await waitFor(() => fs.existsSync(bearerFile), "Bearer fixture was not created");
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/schedule`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
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
    const key = async (value) => { const keyCode = { Enter: 13, Escape: 27 }[value]; await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: value, code: value, windowsVirtualKeyCode: keyCode }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code: value, windowsVirtualKeyCode: keyCode }); };
    await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable");
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: `Bearer ${fs.readFileSync(bearerFile, "utf8")}`, Origin: "http://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/schedule?role=project-manager&scope=estate&window=latest&baseline=none&service=all&lens=roadmap&timezone=UTC` });
    await waitFor(async () => evaluate("document.querySelectorAll('.schedule-row').length === 2").catch(() => false), "Schedule did not render");
    for (const lens of ["roadmap", "gantt", "flow"]) {
      await evaluate(`document.getElementById("schedule-lens").value=${JSON.stringify(lens)};document.getElementById("schedule-lens").dispatchEvent(new Event("change",{bubbles:true}))`);
      await waitFor(async () => evaluate(`new URL(location.href).searchParams.get("lens") === ${JSON.stringify(lens)} && document.querySelectorAll('.schedule-row').length === 2`).catch(() => false), `Lens ${lens} did not synchronize`);
    }
    await evaluate("document.querySelector('[data-detail]').focus()"); await key("Enter");
    assert.equal(await evaluate("document.getElementById('schedule-detail').open"), true);
    await key("Escape");
    assert.equal(await evaluate("document.activeElement.hasAttribute('data-detail')"), true);
    assert.equal(await evaluate("document.getElementById('schedule-warning').hidden"), false);
    for (const width of [390, 320]) { await send("Emulation.setDeviceMetricsOverride", { width, height: 800, deviceScaleFactor: 1, mobile: true }); const size = JSON.parse(await evaluate("JSON.stringify({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})")); assert.equal(size.scroll <= size.client, true, JSON.stringify(size)); }
    const writes = requests.filter((request) => !["GET", "OPTIONS"].includes(request.method));
    const external = requests.filter((request) => !request.url.startsWith(`http://127.0.0.1:${port}/`));
    assert.deepEqual(writes, []); assert.deepEqual(external, []); assert.deepEqual(exceptions, []);
    console.log("SKCP-21A CDP PASS: Roadmap/Gantt/Flow, 2 items, exception, keyboard detail, 390/320, zero writes/external/exceptions");
    socket.close();
  } finally { server.kill("SIGTERM"); chrome.kill("SIGTERM"); }
}
qualify().catch((error) => { console.error(error); process.exitCode = 1; });
